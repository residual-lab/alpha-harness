#!/bin/sh
# Validate DESIGN.md against the DESIGN.md token format.
#   design-lint.sh <DESIGN.md>
# Run from the repository root.
#
# `design.md lint` exits 0 whatever it finds, and warns for anything its schema does not know:
# an unrecognised component property, a colour no component references, a missing section. A
# token an agent cannot read is a token this file cannot claim to define, so warnings fail here
# too, and an unknown rule from a future release fails by default.
#
# The one exception is `contrast-ratio`, which judges in WCAG 2. This system judges in APCA
# (DESIGN.md → Colors) because WCAG 2 misreads light text on a #010102 canvas, and APCA is the
# stricter of the two on these tokens: ink-subtle passes WCAG AA at 5.86:1 while APCA puts it
# below the body-text floor. Those findings print, every run, but do not fail the build.
set -eu
target=$(cd "$(dirname "${1:?path to DESIGN.md}")" && pwd)/$(basename "$1")

pnpm --silent --dir frontend exec design.md lint --format=json "$target" | node -e '
  let raw = ""
  process.stdin.on("data", (chunk) => (raw += chunk)).on("end", () => {
    const findings = (JSON.parse(raw).findings ?? []).filter((f) => f.severity !== "info")
    const where = (f) => (f.path ? f.path + ": " : "")
    const advisory = findings.filter((f) => f.rule === "contrast-ratio")
    const structural = findings.filter((f) => f.rule !== "contrast-ratio")

    for (const f of advisory) {
      console.error(`advisory (WCAG 2, not this system’s standard): ${where(f)}${f.message}`)
    }
    for (const f of structural) console.error(`${f.severity}: ${where(f)}${f.message}`)

    if (structural.length > 0) {
      console.error(
        `\nDESIGN.md: ${structural.length} issue(s). See: pnpm --dir frontend exec design.md spec`,
      )
      process.exit(1)
    }
  })
'

# The APCA floors themselves, measured from this file's own colours and Floors table.
node frontend/tools/contrast.ts "$target"
