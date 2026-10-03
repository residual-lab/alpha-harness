# Alpha Harness Improved

Alpha Harness Improved is an open-source fork by Vedant V of a local research studio for the WorldQuant BRAIN platform. It runs on your computer. BRAIN credentials are encrypted at rest and do not get sent to the browser.

Project: [github.com/Vedant-Vispute/alpha-harness-improved-](https://github.com/Vedant-Vispute/alpha-harness-improved-)

This guide is written for first-time users. Choose the prebuilt release method unless you are developing Alpha Harness itself.

## Choose an installation method

| Method | Best for | What you need |
| --- | --- | --- |
| Prebuilt release | Most users | A browser and the correct download for your operating system |
| Install from source | Users who want to run the repository directly | Git, uv, Node.js 26, and pnpm; uv manages Python 3.14 |
| Developer mode | Contributors changing the frontend or backend | Everything above, plus two terminal windows |

The prebuilt release is the easiest option. It includes the application and opens Alpha Harness in your browser.

## Method 1: Prebuilt release

Open the [latest Alpha Harness Improved release](https://github.com/Vedant-Vispute/alpha-harness-improved-/releases/latest) in your browser. Download the file for your system.

### macOS Apple Silicon

This is the download for Macs with M1, M2, M3, M4, or later Apple Silicon chips.

1. Open Terminal.
2. Create an installation folder first:

   ```bash
   mkdir -p "$HOME/Applications/alpha_harness_improved"
   cd "$HOME/Applications/alpha_harness_improved"
   ```

3. Download the latest Apple Silicon release:

   ```bash
   curl -L "https://github.com/Vedant-Vispute/alpha-harness-improved-/releases/latest/download/AlphaHarness-macOS-arm64.zip" -o AlphaHarness.zip
   unzip -q AlphaHarness.zip
   rm AlphaHarness.zip
   ```

4. Start Alpha Harness:

   ```bash
   open AlphaHarness.app
   ```

If macOS says that the app cannot be opened, open Finder, right-click `AlphaHarness.app`, choose **Open**, and confirm. The first launch may take a while while the application installs its Python environment and dependencies.

### macOS Intel

Use this method for an Intel Mac. You can check your chip with `Apple menu > About This Mac`.

```bash
mkdir -p "$HOME/Applications/alpha_harness_improved"
cd "$HOME/Applications/alpha_harness_improved"
curl -L "https://github.com/Vedant-Vispute/alpha-harness-improved-/releases/latest/download/AlphaHarness-macOS-x86_64.zip" -o AlphaHarness.zip
unzip -q AlphaHarness.zip
rm AlphaHarness.zip
open AlphaHarness.app
```

### Windows

1. Open PowerShell.
2. Create an installation folder first:

   ```powershell
   New-Item -ItemType Directory -Force "$HOME\alpha_harness_improved" | Out-Null
   Set-Location "$HOME\alpha_harness_improved"
   ```

3. Download the latest Windows launcher:

   ```powershell
   Invoke-WebRequest `
   -Uri "https://github.com/Vedant-Vispute/alpha-harness-improved-/releases/latest/download/AlphaHarness.exe" `
   -OutFile "$HOME\alpha_harness_improved\AlphaHarness.exe"
   ```

4. Start it:

   ```powershell
   Start-Process "$HOME\alpha_harness_improved\AlphaHarness.exe"
   ```

Windows may show a security confirmation the first time. Choose **More info**, then **Run anyway** if you trust the download.

### Linux x86_64

These commands are for standard 64-bit Intel or AMD Linux systems.

```bash
mkdir -p "$HOME/Applications/alpha_harness_improved"
cd "$HOME/Applications/alpha_harness_improved"
curl -L "https://github.com/Vedant-Vispute/alpha-harness-improved-/releases/latest/download/AlphaHarness-linux-x86_64.tar.gz" -o AlphaHarness.tar.gz
tar -xzf AlphaHarness.tar.gz
rm AlphaHarness.tar.gz
chmod +x AlphaHarness
./AlphaHarness
```

The Linux release requires glibc 2.35 or newer, such as Ubuntu 22.04 or a newer distribution.

### After the first launch

Alpha Harness opens at [http://127.0.0.1:8000](http://127.0.0.1:8000). Leave the launcher running while using the application. The launcher manages updates and the local Python environment for you.

The launcher stores its files here:

- macOS: `~/Library/Application Support/alpha_harness_improved`
- Windows: `%LOCALAPPDATA%\alpha_harness_improved`
- Linux: `~/.local/share/alpha_harness_improved`

## Method 2: Install from source

Use this method when you want to run the repository itself instead of downloading a release.

### macOS or Linux

Create a folder first, then clone the repository into it:

```bash
mkdir -p "$HOME/Projects"
cd "$HOME/Projects"
git clone https://github.com/Vedant-Vispute/alpha-harness-improved-.git
cd alpha-harness-improved-
```

Install the required tools if they are not already installed:

```bash
# macOS only: install Homebrew from https://brew.sh if you do not have it.
brew install git uv node

# Linux users can install Git and Node.js through their distribution's package manager.
# Install uv from https://docs.astral.sh/uv/getting-started/installation/ if needed.
```

Install the backend dependencies and build the frontend:

```bash
cd backend
uv sync
cd ../frontend
corepack enable
pnpm install --frozen-lockfile
pnpm build
```

Start the application from the backend directory:

```bash
cd ../backend
uv run alpha-harness
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

### Windows PowerShell

Create a folder first, then clone the repository:

```powershell
New-Item -ItemType Directory -Force "$HOME\Projects" | Out-Null
Set-Location "$HOME\Projects"
git clone https://github.com/Vedant-Vispute/alpha-harness-improved-.git
Set-Location "$HOME\Projects\alpha-harness-improved-"
```

Install `uv` if needed:

```powershell
winget install --id Git.Git
winget install --id OpenJS.NodeJS.LTS
irm https://astral.sh/uv/install.ps1 | iex
```

Close and reopen PowerShell after installing these tools so that the updated `PATH` is loaded.

Install dependencies, build the frontend, and start the application:

```powershell
Set-Location backend
uv sync
Set-Location ..\frontend
corepack enable
pnpm install --frozen-lockfile
pnpm build
Set-Location ..\backend
uv run alpha-harness
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

## Method 3: Developer mode

Developer mode runs the backend and the Vite frontend separately. It gives frontend changes fast reloads.

Create the repository folder and install dependencies using one of the source-install methods above. Then open two terminals.

In terminal 1, start the backend:

```bash
cd "$HOME/Projects/alpha-harness/backend"
uv run uvicorn alpha_harness.main:app --reload --port 8000
```

In terminal 2, start Vite:

```bash
cd "$HOME/Projects/alpha-harness/frontend"
pnpm dev
```

Open the URL printed by Vite, usually [http://localhost:5173](http://localhost:5173). The frontend talks to the backend on port `8000`.

On Windows, use the same commands in PowerShell with Windows paths:

```powershell
Set-Location "$HOME\Projects\alpha-harness\backend"
uv run uvicorn alpha_harness.main:app --reload --port 8000
```

In the second PowerShell window:

```powershell
Set-Location "$HOME\Projects\alpha-harness\frontend"
pnpm dev
```

## Updating

For a prebuilt release, download the newest launcher for your operating system and replace the old launcher in the same folder. The application can also notify you when an update is available.

For a source installation:

```bash
cd "$HOME/Projects/alpha-harness"
git pull
cd backend
uv sync
cd ../frontend
pnpm install --frozen-lockfile
pnpm build
```

## Troubleshooting

### The browser page does not open

Make sure Alpha Harness is still running, then open [http://127.0.0.1:8000](http://127.0.0.1:8000) manually. Only one Alpha Harness instance can use port `8000` at a time.

### macOS blocks the application

Right-click `AlphaHarness.app`, choose **Open**, and confirm. Do not download applications from unofficial mirrors.

### The first launch seems slow

The first launch installs a managed Python runtime and the application dependencies. Later launches are much faster.

### I am not sure which Mac download to use

Run this command:

```bash
uname -m
```

Use `arm64` for Apple Silicon and `x86_64` for Intel.

### Stop the application

Close the Alpha Harness window or stop the terminal process with `Ctrl+C` when running from source.

## Privacy

Alpha Harness is designed to run locally. Credentials are encrypted at rest and are not sent to the browser. Keep your operating system account and downloaded release files secure.