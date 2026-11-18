//! Fast Expression, read the way the backend's `labs/fastexpr.py` reads it, so a screen in the
//! browser and a lab on the backend never disagree about an Alpha. The two are checked against
//! each other on every Alpha in the vault, the template gallery and generated expressions.
//!
//! Precedence, loosest first: ternary, `||`, `&&`, comparisons, `+ -`, `* /`, `^`
//! (right-associative), unary `-`/`!`, calls. A sign binds tighter than `^`, as on BRAIN:
//! `-x ^ 0.5` is `(-x) ^ 0.5`.
//!
//! A position in a message counts characters, as Python does, so a message is word for word
//! the backend's. Every `at` counts UTF-16 units, as the editor does.

use std::fmt::Write;

/// How deep an expression may go, each call, bracket, sign, power and chained operator a level.
/// Python's parser runs out of stack not much past it; a real Alpha goes a dozen deep.
pub const MAX_DEPTH: usize = 64;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Num,
    Str,
    Name,
    Call,
    Unary,
    Binary,
    Ternary,
    Assign,
    Seq,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Num => "num",
            Kind::Str => "str",
            Kind::Name => "name",
            Kind::Call => "call",
            Kind::Unary => "unary",
            Kind::Binary => "binary",
            Kind::Ternary => "ternary",
            Kind::Assign => "assign",
            Kind::Seq => "seq",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Node {
    pub kind: Kind,
    pub value: String,
    pub args: Vec<Node>,
    pub kwargs: Vec<(String, Node)>,
    /// Where its own token starts: a call's name, an operator, a leaf.
    pub at: usize,
}

impl Node {
    fn new(kind: Kind, value: &str, args: Vec<Node>, at: usize) -> Node {
        Node {
            kind,
            value: value.to_owned(),
            args,
            kwargs: Vec::new(),
            at,
        }
    }
}

#[derive(Debug, PartialEq)]
pub struct ParseError {
    pub message: String,
    /// The position the message names, or where reading stopped when it names none.
    pub at: usize,
}

/// Selection Expressions read Fast Expression over an Alpha's metrics: `25%` and `5bps` are
/// numbers, a line break ends a statement, and a sign may be `+`.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Dialect {
    FastExpression,
    Selection,
}

// -- characters -----------------------------------------------------------------------------

/// Python's `str.isspace()`, which its `\s` and `rstrip()` follow: Unicode's White_Space and
/// the four ASCII separators.
fn space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's `str.isprintable()`, near enough without Unicode's tables: what `repr()` escapes.
fn printable(c: char) -> bool {
    !(c.is_control()
        || (c.is_whitespace() && c != ' ')
        || matches!(c, '\u{ad}' | '\u{200b}'..='\u{200f}' | '\u{202a}'..='\u{202e}')
        || matches!(c, '\u{2060}'..='\u{2064}' | '\u{2066}'..='\u{206f}' | '\u{feff}')
        || matches!(c, '\u{e000}'..='\u{f8ff}' | '\u{f0000}'..))
}

/// Python's `repr()` of a string, which its messages quote tokens with.
pub fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if !printable(c) => {
                let n = c as u32;
                let _ = match n {
                    0..0x100 => write!(out, "\\x{n:02x}"),
                    0x100..0x10000 => write!(out, "\\u{n:04x}"),
                    _ => write!(out, "\\U{n:08x}"),
                };
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Characters and UTF-16 units up to a byte, walked forward only.
struct Cursor<'a> {
    text: &'a str,
    byte: usize,
    chars: usize,
    units: usize,
}

impl<'a> Cursor<'a> {
    fn new(text: &'a str) -> Self {
        Cursor {
            text,
            byte: 0,
            chars: 0,
            units: 0,
        }
    }

    fn seek(&mut self, byte: usize) -> (usize, usize) {
        for c in self.text[self.byte..byte].chars() {
            self.chars += 1;
            self.units += c.len_utf16();
        }
        self.byte = byte;
        (self.chars, self.units)
    }
}

fn find(bytes: &[u8], from: usize, needle: &[u8]) -> Option<usize> {
    bytes[from..]
        .windows(needle.len())
        .position(|w| w == needle)
        .map(|p| p + from)
}

// -- tokens ---------------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tok {
    Num,
    Str,
    Name,
    Op,
    Comment,
    Invalid,
}

#[derive(Clone, Copy, Debug)]
struct Token {
    kind: Tok,
    start: usize,
    end: usize,
    chars: usize,
    units: usize,
    /// A line break ending a Selection Expression's statement, read as `;`.
    newline: bool,
}

fn digits(b: &[u8], mut i: usize) -> usize {
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    i
}

/// `(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?`, and in a Selection Expression `(?:%|bps)?` after.
fn number(b: &[u8], start: usize, dialect: Dialect) -> Option<usize> {
    let mut i = if b[start].is_ascii_digit() {
        let i = digits(b, start);
        if b.get(i) == Some(&b'.') {
            digits(b, i + 1)
        } else {
            i
        }
    } else if b[start] == b'.' && b.get(start + 1).is_some_and(u8::is_ascii_digit) {
        digits(b, start + 1)
    } else {
        return None;
    };
    if matches!(b.get(i), Some(b'e' | b'E')) {
        let sign = usize::from(matches!(b.get(i + 1), Some(b'+' | b'-')));
        if b.get(i + 1 + sign).is_some_and(u8::is_ascii_digit) {
            i = digits(b, i + 1 + sign);
        }
    }
    if dialect == Dialect::Selection {
        if b.get(i) == Some(&b'%') {
            i += 1;
        } else if b[i..].starts_with(b"bps") {
            i += 3;
        }
    }
    Some(i)
}

/// `"[^"]*"|'[^']*'`
fn string(b: &[u8], start: usize) -> Option<usize> {
    let quote = b[start];
    if quote != b'"' && quote != b'\'' {
        return None;
    }
    find(b, start + 1, &[quote]).map(|end| end + 1)
}

/// `\$?[A-Za-z_][A-Za-z0-9_]*|\.\.\.`: Template Lab's `$variable` and its `...` hole too.
fn name(b: &[u8], start: usize) -> Option<usize> {
    if b[start..].starts_with(b"...") {
        return Some(start + 3);
    }
    let first = start + usize::from(b[start] == b'$');
    if !b
        .get(first)
        .is_some_and(|&c| c.is_ascii_alphabetic() || c == b'_')
    {
        return None;
    }
    let mut i = first + 1;
    while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == b'_') {
        i += 1;
    }
    Some(i)
}

/// `&&|\|\||==|!=|<=|>=|[-+*/^<>!?:(),=;]`
fn op(b: &[u8], start: usize) -> Option<usize> {
    let pair = b.get(start..start + 2);
    if matches!(pair, Some(b"&&" | b"||" | b"==" | b"!=" | b"<=" | b">=")) {
        return Some(start + 2);
    }
    b"-+*/^<>!?:(),=;".contains(&b[start]).then_some(start + 1)
}

/// Comments, as byte ranges: `/* */`, `//` and `#` to the line's end, never inside a string.
fn comments(b: &[u8]) -> Vec<(usize, usize)> {
    let mut found = Vec::new();
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'"' | b'\'' => {
                if let Some(end) = string(b, i) {
                    i = end;
                    continue;
                }
            }
            b'/' if b.get(i + 1) == Some(&b'*') => {
                if let Some(end) = find(b, i + 2, b"*/") {
                    found.push((i, end + 2));
                    i = end + 2;
                    continue;
                }
            }
            b'/' | b'#' if b[i] == b'#' || b.get(i + 1) == Some(&b'/') => {
                let end = find(b, i, b"\n").unwrap_or(b.len());
                found.push((i, end));
                i = end;
                continue;
            }
            _ => {}
        }
        i += 1;
    }
    found
}

fn in_any(ranges: &[(usize, usize)], at: usize) -> bool {
    let after = ranges.partition_point(|&(start, _)| start <= at);
    after > 0 && at < ranges[after - 1].1
}

struct Tokens {
    list: Vec<Token>,
    /// Where the text ends once trailing space and comments are dropped.
    end: (usize, usize),
}

fn tokenize(text: &str, dialect: Dialect) -> Result<Tokens, ParseError> {
    let b = text.as_bytes();
    let blank = comments(b);
    let end = text
        .char_indices()
        .rfind(|&(i, c)| !space(c) && !in_any(&blank, i))
        .map_or(0, |(i, c)| i + c.len_utf8());
    let mut cursor = Cursor::new(text);
    let mut list = Vec::new();
    let mut position = 0;
    while position < end {
        let mut at = position;
        while let Some(c) = text[at..].chars().next() {
            let separator = dialect == Dialect::Selection && c == '\n';
            if separator || !(space(c) || in_any(&blank, at)) {
                break;
            }
            at += c.len_utf8();
        }
        if at >= end {
            break;
        }
        let newline = b[at] == b'\n';
        let found = if newline {
            Some((Tok::Op, at + 1))
        } else {
            number(b, at, dialect)
                .map(|e| (Tok::Num, e))
                .or_else(|| string(b, at).map(|e| (Tok::Str, e)))
                .or_else(|| name(b, at).map(|e| (Tok::Name, e)))
                .or_else(|| op(b, at).map(|e| (Tok::Op, e)))
        };
        let Some((kind, stop)) = found else {
            let (chars, units) = cursor.seek(position);
            let seen: String = text[position..end]
                .char_indices()
                .map(|(i, c)| if in_any(&blank, position + i) { ' ' } else { c })
                .take(12)
                .collect();
            let message = format!("Cannot read {} at {chars}.", py_repr(&seen));
            return Err(ParseError { message, at: units });
        };
        let (chars, units) = cursor.seek(at);
        list.push(Token {
            kind,
            start: at,
            end: stop,
            chars,
            units,
            newline,
        });
        position = stop;
    }
    Ok(Tokens {
        list,
        end: cursor.seek(end),
    })
}

/// Every token of any text, comments and what cannot be read included, for colouring: never
/// fails. A string or block comment left open runs to the end, as it does while being typed.
/// `(kind, from, to)` in UTF-16 units.
pub fn lex(text: &str) -> Vec<(Tok, usize, usize)> {
    let b = text.as_bytes();
    let mut cursor = Cursor::new(text);
    let mut found = Vec::new();
    let mut i = 0;
    while let Some(c) = text[i..].chars().next() {
        if space(c) {
            i += c.len_utf8();
            continue;
        }
        let (kind, stop) = if b[i..].starts_with(b"/*") {
            (
                Tok::Comment,
                find(b, i + 2, b"*/").map_or(b.len(), |end| end + 2),
            )
        } else if c == '#' || b[i..].starts_with(b"//") {
            (Tok::Comment, find(b, i, b"\n").unwrap_or(b.len()))
        } else if c == '"' || c == '\'' {
            (Tok::Str, string(b, i).unwrap_or(b.len()))
        } else if let Some(end) = number(b, i, Dialect::FastExpression) {
            (Tok::Num, end)
        } else if let Some(end) = name(b, i) {
            (Tok::Name, end)
        } else if let Some(end) = op(b, i) {
            (Tok::Op, end)
        } else {
            (Tok::Invalid, i + c.len_utf8())
        };
        let (_, from) = cursor.seek(i);
        let (_, to) = cursor.seek(stop);
        found.push((kind, from, to));
        i = stop;
    }
    found
}

// -- reading --------------------------------------------------------------------------------

const LEVELS: [&[&str]; 5] = [
    &["||"],
    &["&&"],
    &["==", "!=", "<", "<=", ">", ">="],
    &["+", "-"],
    &["*", "/"],
];

struct Parser<'a> {
    text: &'a str,
    tokens: Vec<Token>,
    index: usize,
    depth: usize,
    dialect: Dialect,
    end: (usize, usize),
}

/// The tree for `text`, or where it stopped making sense.
pub fn parse(text: &str, dialect: Dialect) -> Result<Node, ParseError> {
    let Tokens { list, end } = tokenize(text, dialect)?;
    Parser {
        text,
        tokens: list,
        index: 0,
        depth: 0,
        dialect,
        end,
    }
    .program()
}

impl Parser<'_> {
    fn text_of(&self, token: &Token) -> &str {
        &self.text[token.start..token.end]
    }

    fn at(&self, symbol: &str, offset: usize) -> bool {
        self.tokens
            .get(self.index + offset)
            .is_some_and(|token| self.is(token, symbol))
    }

    fn is(&self, token: &Token, symbol: &str) -> bool {
        token.kind == Tok::Op && (self.text_of(token) == symbol || (token.newline && symbol == ";"))
    }

    fn error<T>(&self, message: String, at: usize) -> Result<T, ParseError> {
        Err(ParseError { message, at })
    }

    fn take(&mut self) -> Result<Token, ParseError> {
        let Some(&token) = self.tokens.get(self.index) else {
            return self.error("The expression ends too early.".into(), self.end.1);
        };
        self.index += 1;
        Ok(token)
    }

    fn expect(&mut self, symbol: &str) -> Result<(), ParseError> {
        let token = self.take()?;
        if self.is(&token, symbol) {
            return Ok(());
        }
        let found = if token.newline {
            "the end of the line".into()
        } else {
            py_repr(self.text_of(&token))
        };
        let message = format!(
            "Expected {} at {}, found {found}.",
            py_repr(symbol),
            token.chars
        );
        self.error(message, token.units)
    }

    /// One level deeper, refused past [`MAX_DEPTH`].
    fn deeper(&mut self) -> Result<(), ParseError> {
        self.depth += 1;
        if self.depth <= MAX_DEPTH {
            return Ok(());
        }
        let at = self
            .tokens
            .get(self.index)
            .map_or(self.end.1, |token| token.units);
        self.error("The expression nests too deeply.".into(), at)
    }

    fn program(&mut self) -> Result<Node, ParseError> {
        let mut statements = Vec::new();
        while self.index < self.tokens.len() {
            if self.at(";", 0) {
                self.index += 1;
                continue;
            }
            statements.push(self.statement()?);
            if self.index < self.tokens.len() {
                self.expect(";")?;
            }
        }
        match statements.len() {
            0 => self.error("The expression is empty.".into(), self.end.1),
            1 => Ok(statements.pop().expect("one statement")),
            _ => Ok(Node::new(Kind::Seq, "", statements, 0)),
        }
    }

    fn statement(&mut self) -> Result<Node, ParseError> {
        let token = self.tokens[self.index];
        if token.kind == Tok::Name && self.at("=", 1) {
            self.index += 2;
            let value = self.expr()?;
            return Ok(Node::new(
                Kind::Assign,
                self.text_of(&token),
                vec![value],
                token.units,
            ));
        }
        self.expr()
    }

    fn expr(&mut self) -> Result<Node, ParseError> {
        self.deeper()?;
        let mut condition = self.binary(0)?;
        if self.at("?", 0) {
            let mark = self.take()?;
            let yes = self.expr()?;
            self.expect(":")?;
            let no = self.expr()?;
            condition = Node::new(Kind::Ternary, "", vec![condition, yes, no], mark.units);
        }
        self.depth -= 1;
        Ok(condition)
    }

    fn binary(&mut self, level: usize) -> Result<Node, ParseError> {
        if level == LEVELS.len() {
            return self.power();
        }
        let mut left = self.binary(level + 1)?;
        let mut chained = 0;
        while let Some(token) = self.tokens.get(self.index).copied() {
            let symbol = self.text_of(&token);
            if token.kind != Tok::Op || !LEVELS[level].contains(&symbol) {
                break;
            }
            self.index += 1;
            chained += 1;
            self.deeper()?;
            let right = self.binary(level + 1)?;
            left = Node::new(
                Kind::Binary,
                self.text_of(&token),
                vec![left, right],
                token.units,
            );
        }
        self.depth -= chained;
        Ok(left)
    }

    fn power(&mut self) -> Result<Node, ParseError> {
        let base = self.unary()?;
        if !self.at("^", 0) {
            return Ok(base);
        }
        let mark = self.take()?;
        self.deeper()?;
        let exponent = self.power()?;
        self.depth -= 1;
        Ok(Node::new(
            Kind::Binary,
            "^",
            vec![base, exponent],
            mark.units,
        ))
    }

    fn unary(&mut self) -> Result<Node, ParseError> {
        let plus = self.dialect == Dialect::Selection && self.at("+", 0);
        if !(self.at("-", 0) || self.at("!", 0) || plus) {
            return self.primary();
        }
        let mark = self.take()?;
        self.deeper()?;
        let inner = self.unary()?;
        self.depth -= 1;
        Ok(Node::new(
            Kind::Unary,
            self.text_of(&mark),
            vec![inner],
            mark.units,
        ))
    }

    fn primary(&mut self) -> Result<Node, ParseError> {
        let token = self.take()?;
        let text = self.text_of(&token);
        match token.kind {
            Tok::Num => Ok(Node::new(Kind::Num, text, Vec::new(), token.units)),
            Tok::Str => Ok(Node::new(Kind::Str, text, Vec::new(), token.units)),
            Tok::Name if self.at("(", 0) => self.call(token),
            Tok::Name => Ok(Node::new(Kind::Name, text, Vec::new(), token.units)),
            _ if text == "(" && !token.newline => {
                let inner = self.expr()?;
                self.expect(")")?;
                Ok(inner)
            }
            _ if token.newline => self.error(
                format!("The line ends too early at {}.", token.chars),
                token.units,
            ),
            _ => self.error(
                format!("Unexpected {} at {}.", py_repr(text), token.chars),
                token.units,
            ),
        }
    }

    fn call(&mut self, name: Token) -> Result<Node, ParseError> {
        self.expect("(")?;
        let mut node = Node::new(Kind::Call, self.text_of(&name), Vec::new(), name.units);
        if self.at(")", 0) {
            self.index += 1;
            return Ok(node);
        }
        loop {
            match self.tokens.get(self.index) {
                Some(&key) if key.kind == Tok::Name && self.at("=", 1) => {
                    self.index += 2;
                    let value = self.expr()?;
                    node.kwargs.push((self.text_of(&key).to_owned(), value));
                }
                _ => {
                    let value = self.expr()?;
                    node.args.push(value);
                }
            }
            if self.at(",", 0) {
                self.index += 1;
                continue;
            }
            self.expect(")")?;
            return Ok(node);
        }
    }
}

// -- writing --------------------------------------------------------------------------------

const TERNARY: u8 = 1;
const UNARY: u8 = 8;
const ATOM: u8 = 9;

fn binding(symbol: &str) -> u8 {
    match symbol {
        "||" => 2,
        "&&" => 3,
        "+" | "-" => 5,
        "*" | "/" => 6,
        "^" => 7,
        _ => 4,
    }
}

fn precedence(node: &Node) -> u8 {
    match node.kind {
        Kind::Binary => binding(&node.value),
        Kind::Unary => UNARY,
        Kind::Ternary => TERNARY,
        Kind::Assign | Kind::Seq => 0,
        _ => ATOM,
    }
}

fn wrapped(out: &mut String, node: &Node, parenthesise: bool, separator: &str) {
    if parenthesise {
        out.push('(');
    }
    write_node(out, node, separator);
    if parenthesise {
        out.push(')');
    }
}

fn write_node(out: &mut String, node: &Node, separator: &str) {
    match node.kind {
        Kind::Num | Kind::Str | Kind::Name => out.push_str(&node.value),
        Kind::Call => {
            out.push_str(&node.value);
            out.push('(');
            let mut first = true;
            for arg in &node.args {
                if !first {
                    out.push_str(", ");
                }
                first = false;
                write_node(out, arg, separator);
            }
            for (key, value) in &node.kwargs {
                if !first {
                    out.push_str(", ");
                }
                first = false;
                out.push_str(key);
                out.push('=');
                write_node(out, value, separator);
            }
            out.push(')');
        }
        Kind::Unary => {
            out.push_str(&node.value);
            let child = &node.args[0];
            wrapped(out, child, precedence(child) < UNARY, separator);
        }
        Kind::Binary => {
            let own = binding(&node.value);
            let (left, right) = (&node.args[0], &node.args[1]);
            // `^` binds to the right; every other operator to the left.
            let (lp, rp) = if node.value == "^" {
                (precedence(left) <= own, precedence(right) < own)
            } else {
                (precedence(left) < own, precedence(right) <= own)
            };
            wrapped(out, left, lp, separator);
            out.push(' ');
            out.push_str(&node.value);
            out.push(' ');
            wrapped(out, right, rp, separator);
        }
        Kind::Ternary => {
            let condition = &node.args[0];
            wrapped(out, condition, precedence(condition) <= TERNARY, separator);
            out.push_str(" ? ");
            write_node(out, &node.args[1], separator);
            out.push_str(" : ");
            write_node(out, &node.args[2], separator);
        }
        Kind::Assign => {
            out.push_str(&node.value);
            out.push_str(" = ");
            write_node(out, &node.args[0], separator);
        }
        Kind::Seq => {
            for (i, statement) in node.args.iter().enumerate() {
                if i > 0 {
                    out.push_str(separator);
                }
                write_node(out, statement, separator);
            }
        }
    }
}

/// The tree as text, bracketed exactly where the grammar needs it; statements joined by
/// `separator`, which the backend writes as `"; "`.
pub fn render(node: &Node, separator: &str) -> String {
    let mut out = String::new();
    write_node(&mut out, node, separator);
    out
}

// -- JSON -----------------------------------------------------------------------------------

fn json_string(out: &mut String, text: &str) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Without `core::fmt`, which measured as most of the time spent writing a tree out.
fn json_number(out: &mut String, mut n: usize) {
    let mut digits = [0u8; 20];
    let mut at = digits.len();
    loop {
        at -= 1;
        digits[at] = b'0' + (n % 10) as u8;
        n /= 10;
        if n == 0 {
            break;
        }
    }
    out.extend(digits[at..].iter().map(|&d| char::from(d)));
}

fn json_node(out: &mut String, node: &Node) {
    out.push_str("{\"kind\":\"");
    out.push_str(node.kind.name());
    out.push_str("\",\"value\":");
    json_string(out, &node.value);
    out.push_str(",\"at\":");
    json_number(out, node.at);
    out.push_str(",\"args\":[");
    for (i, arg) in node.args.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        json_node(out, arg);
    }
    out.push_str("],\"kwargs\":[");
    for (i, (key, value)) in node.kwargs.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str("{\"name\":");
        json_string(out, key);
        out.push_str(",\"value\":");
        json_node(out, value);
        out.push('}');
    }
    out.push_str("]}");
}

fn json_error(error: &ParseError) -> String {
    let mut out = String::from("{\"error\":");
    json_string(&mut out, &error.message);
    out.push_str(",\"at\":");
    json_number(&mut out, error.at);
    out.push('}');
    out
}

/// `{"tree": …}` or `{"error": …, "at": …}`.
pub fn parse_json(text: &str, dialect: Dialect) -> String {
    match parse(text, dialect) {
        Ok(tree) => {
            let mut out = String::from("{\"tree\":");
            json_node(&mut out, &tree);
            out.push('}');
            out
        }
        Err(error) => json_error(&error),
    }
}

/// `{"text": …}`, the expression read and written back, or `{"error": …, "at": …}`.
pub fn render_json(text: &str, separator: &str) -> String {
    match parse(text, Dialect::FastExpression) {
        Ok(tree) => {
            let mut out = String::from("{\"text\":");
            json_string(&mut out, &render(&tree, separator));
            out.push('}');
            out
        }
        Err(error) => json_error(&error),
    }
}

/// [`lex`] as little-endian `u32` triples, `kind, from, to`, `kind` counting from `Tok::Num`:
/// read in place rather than parsed, as the editor reads them on every keystroke.
pub fn tokens_bytes(text: &str) -> Vec<u8> {
    lex(text)
        .into_iter()
        .flat_map(|(kind, from, to)| [kind as u32, from as u32, to as u32])
        .flat_map(u32::to_le_bytes)
        .collect()
}

// -- WebAssembly ----------------------------------------------------------------------------

/// The module's whole interface: the caller writes UTF-8 into memory from `allocate`, calls a
/// function with its pointer and length, and reads back what it returns: a little-endian `u32`
/// length, then JSON, or for `tokens` the triples. Both are given back with `release`.
#[cfg(target_arch = "wasm32")]
mod exports {
    use super::{Dialect, parse_json, render_json, tokens_bytes};
    use std::alloc::{Layout, alloc, dealloc};

    #[unsafe(no_mangle)]
    pub extern "C" fn allocate(len: usize) -> *mut u8 {
        // Never zero-sized, which `alloc` does not allow.
        unsafe { alloc(Layout::array::<u8>(len.max(1)).expect("a buffer")) }
    }

    /// # Safety
    /// `ptr` came from `allocate(len)` or is a result whose length prefix said `len - 4`.
    #[unsafe(no_mangle)]
    pub unsafe extern "C" fn release(ptr: *mut u8, len: usize) {
        unsafe { dealloc(ptr, Layout::array::<u8>(len.max(1)).expect("a buffer")) }
    }

    fn input<'a>(ptr: *const u8, len: usize) -> &'a str {
        let bytes = unsafe { std::slice::from_raw_parts(ptr, len) };
        // The callers encode from strings; anything else reads as nothing rather than trapping.
        std::str::from_utf8(bytes).unwrap_or("")
    }

    fn output(bytes: &[u8]) -> *mut u8 {
        let ptr = allocate(bytes.len() + 4);
        unsafe {
            ptr.cast::<[u8; 4]>()
                .write_unaligned((bytes.len() as u32).to_le_bytes());
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), ptr.add(4), bytes.len());
        }
        ptr
    }

    /// `selection` is 1 for a Selection Expression.
    #[unsafe(no_mangle)]
    pub extern "C" fn parse(ptr: *const u8, len: usize, selection: u32) -> *mut u8 {
        let dialect = if selection == 1 {
            Dialect::Selection
        } else {
            Dialect::FastExpression
        };
        output(parse_json(input(ptr, len), dialect).as_bytes())
    }

    /// `lines` is 1 to put each statement on its own line, 0 for the backend's `"; "`.
    #[unsafe(no_mangle)]
    pub extern "C" fn render(ptr: *const u8, len: usize, lines: u32) -> *mut u8 {
        let separator = if lines == 1 { ";\n" } else { "; " };
        output(render_json(input(ptr, len), separator).as_bytes())
    }

    #[unsafe(no_mangle)]
    pub extern "C" fn tokens(ptr: *const u8, len: usize) -> *mut u8 {
        output(&tokens_bytes(input(ptr, len)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn read(text: &str) -> String {
        render(&parse(text, Dialect::FastExpression).expect(text), "; ")
    }

    fn fails(text: &str) -> String {
        parse(text, Dialect::FastExpression)
            .expect_err(text)
            .message
    }

    #[test]
    fn a_sign_binds_tighter_than_a_power() {
        let tree = parse("-2 ^ 2", Dialect::FastExpression).unwrap();
        assert_eq!((tree.kind, tree.args[0].kind), (Kind::Binary, Kind::Unary));
        assert_eq!(read("-(x ^ y)"), "-(x ^ y)");
        assert_eq!(read("(-x) ^ y"), "-x ^ y");
        assert_eq!(read("2 ^ -1"), "2 ^ -1");
        assert_eq!(read("2 ^ 3 ^ 2"), "2 ^ 3 ^ 2");
        assert_eq!(read("(2 ^ 3) ^ 2"), "(2 ^ 3) ^ 2");
    }

    #[test]
    fn writes_back_what_it_reads() {
        for (text, want) in [
            ("rank(close)", "rank(close)"),
            ("a - (b - c)", "a - (b - c)"),
            ("(a - b) - c", "a - b - c"),
            ("x > 0 ? 1 : -1", "x > 0 ? 1 : -1"),
            ("(a ? b : c) + 1", "(a ? b : c) + 1"),
            (
                "a = rank(close);\nb = a * a;\nb - a",
                "a = rank(close); b = a * a; b - a",
            ),
            ("f(k = 1, x)", "f(x, k=1)"),
            ("$op($f, ...)", "$op($f, ...)"),
            (
                "bucket(rank(cap), range=\"0, 1, 0.1\")",
                "bucket(rank(cap), range=\"0, 1, 0.1\")",
            ),
            ("rank(close) // momentum", "rank(close)"),
            ("/* note */ a = 1; # why\n a", "a = 1; a"),
            ("x\u{a0}+\u{3000}1", "x + 1"),
        ] {
            assert_eq!(read(text), want, "{text}");
        }
    }

    #[test]
    fn says_where_it_stopped_as_python_does() {
        assert_eq!(fails("rank(close"), "The expression ends too early.");
        assert_eq!(fails("rank(close))"), "Expected ';' at 11, found ')'.");
        assert_eq!(fails("f(x \"s\")"), "Expected ')' at 4, found '\"s\"'.");
        assert_eq!(fails("x + * y"), "Unexpected '*' at 4.");
        assert_eq!(fails("x @ y"), "Cannot read ' @ y' at 1.");
        assert_eq!(fails("rank(x) /* open"), "Unexpected '*' at 9.");
        assert_eq!(fails(" ; ;"), "The expression is empty.");
        assert_eq!(fails("é + x"), "Cannot read 'é + x' at 0.");
        let deep = format!("{}x{}", "rank(".repeat(MAX_DEPTH), ")".repeat(MAX_DEPTH));
        assert_eq!(fails(&deep), "The expression nests too deeply.");
        assert!(parse(&deep[5..deep.len() - 1], Dialect::FastExpression).is_ok());
    }

    #[test]
    fn comments_end_outside_strings() {
        assert_eq!(read("f(x, s = \"a # b\")"), "f(x, s=\"a # b\")");
        assert_eq!(read("x /* \" */ + 1"), "x + 1");
    }

    #[test]
    fn selection_reads_lines_percent_and_bps() {
        let tree = parse(
            "sharpe > 1\n# note\nmargin >= 5bps; returns > 25%",
            Dialect::Selection,
        );
        let tree = tree.unwrap();
        assert_eq!((tree.kind, tree.args.len()), (Kind::Seq, 3));
        assert_eq!(tree.args[1].args[1].value, "5bps");
        let line = parse("sharpe > (1\nfitness > 1", Dialect::Selection).unwrap_err();
        assert_eq!(
            line.message,
            "Expected ')' at 11, found the end of the line."
        );
        assert!(parse("+1", Dialect::FastExpression).is_err());
        assert!(parse("x > +1", Dialect::Selection).is_ok());
    }

    #[test]
    fn positions_count_utf16_for_the_editor() {
        // An emoji is one character in the message and two units in `at`.
        let error = parse("/* 😀 */ x y", Dialect::FastExpression).unwrap_err();
        assert_eq!(
            (error.message.as_str(), error.at),
            ("Expected ';' at 10, found 'y'.", 11)
        );
        assert_eq!(lex("😀 x")[1], (Tok::Name, 3, 4));
    }

    #[test]
    fn lexing_never_fails() {
        let kinds: Vec<_> = lex("f(\"open /* x @").into_iter().map(|t| t.0).collect();
        assert_eq!(kinds, [Tok::Name, Tok::Op, Tok::Str]);
        let kinds: Vec<_> = lex("x @ /* open").into_iter().map(|t| t.0).collect();
        assert_eq!(kinds, [Tok::Name, Tok::Invalid, Tok::Comment]);
    }
}
