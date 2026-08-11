#!/usr/bin/env python3
"""
Build a Daytona sandbox Docker image for Computer Use / visual GUI automation.

This image extends daytonaio/langchain-open-swe:0.1.0 and pre-installs the
full desktop environment stack required by Daytona Computer Use:
  - Xvfb (virtual framebuffer)
  - XFCE4 (lightweight desktop)
  - x11vnc (VNC server)
  - novnc (web-based VNC client)
  - Firefox ESR (browser for web interaction)
  - xdotool, xclip (GUI automation helpers)

Usage (on GitHub Codespaces or any Linux machine with Docker):
    export DOCKER_USERNAME=yourusername
    export DOCKER_PASSWORD=dckr_pat_xxx
    export DAYTONA_API_KEY=dtn_xxx
    python3 computer-use-image.py

Or run directly and you'll be prompted for any missing env vars.
"""

import os
import subprocess
import sys
import textwrap
import shutil
from pathlib import Path

# ─────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────
BASE_IMAGE = "daytonaio/langchain-open-swe:0.1.0"
SANDBOX_HOME = "/home/daytona"

BUILD_DIR = Path("/tmp/syntera-computer-use-sandbox-build")


def get_env(key: str, prompt: str = None) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        if prompt:
            val = input(prompt).strip()
        if not val:
            print(f"✗ {key} is required")
            sys.exit(1)
    return val


# ─────────────────────────────────────────────────────────
# Dockerfile generation
# ─────────────────────────────────────────────────────────
def generate_dockerfile():
    dockerfile = textwrap.dedent(f"""\
        FROM {BASE_IMAGE}

        USER root

        ENV DEBIAN_FRONTEND=noninteractive

        # The base image ships a leftover Yarn apt source (dl.yarnpkg.com) whose
        # GPG key isn't installed, which makes `apt-get update` hard-fail on
        # Debian bookworm (unsigned repo = error, not just a warning). Node/Yarn
        # are already installed in the base image, so the repo isn't needed.
        RUN find /etc/apt/sources.list.d/ -type f -exec grep -l 'yarnpkg' {{}} \\; 2>/dev/null | xargs -r rm -f

        # ── Desktop environment stack required by Daytona Computer Use ──
        #    Xvfb: virtual framebuffer X server (no physical display needed)
        #    xfce4: lightweight desktop environment + terminal
        #    x11vnc: VNC server to expose the Xvfb display remotely
        #    novnc: web-based VNC client (browser-accessible desktop)
        #    firefox-esr: browser for web page interaction
        #    dbus-x11: D-Bus session bus (xfce4 needs it)
        #    xdotool: CLI X11 automation (mouse/keyboard synthesis)
        #    xclip: clipboard management
        #    xauth: X authority file management
        #    xfonts-*: font packages for proper rendering
        #    wmctrl: window manager control — used by openBrowser() to detect
        #            whether a browser window actually opened (wmctrl -l).
        #            Without this the agent cannot verify the browser launched
        #            and the entire computer-use flow aborts.
        #    libdbus-glib-1-2: D-Bus GLib bindings — Firefox needs this at
        #            runtime but it is not always pulled in as a hard dependency
        #            with --no-install-recommends on minimal Debian.
        #    libxtst6: X Test extension library — required by xdotool for
        #            mouse/keyboard event synthesis.
        #    libcanberra-gtk3-module: GTK3 sound module — Firefox recommends
        #            it; without it Firefox prints warnings that can stall startup.
        #    procps: provides ps/kill — needed for process cleanup inside the sandbox.
        #    fonts-liberation: metric-compatible web fonts for correct rendering.
        RUN apt-get update && apt-get install -y --no-install-recommends \\
                xvfb \\
                xfce4 \\
                xfce4-terminal \\
                x11vnc \\
                novnc \\
                firefox-esr \\
                dbus-x11 \\
                xdotool \\
                xclip \\
                xauth \\
                xfonts-base \\
                xfonts-100dpi \\
                xfonts-75dpi \\
                curl \\
                ca-certificates \\
                wmctrl \\
                libdbus-glib-1-2 \\
                libxtst6 \\
                libcanberra-gtk3-module \\
                procps \\
                fonts-liberation \\
            && rm -rf /var/lib/apt/lists/*

        # ── Verify no missing shared libraries for firefox-esr ──
        RUN ldd "$(which firefox-esr)" 2>&1 | grep "not found" && exit 1 || echo "All firefox-esr deps satisfied"

        # ── Computer Use environment variables ──
        #    VNC_RESOLUTION: virtual desktop size (per Daytona docs).
        #    DISPLAY is managed by Daytona's computerUse.start() — do NOT
        #    hardcode it here, the agent sets it when starting Xvfb.
        #    MOZ_DISABLE_CONTENT_SANDBOX: Firefox's content sandbox uses
        #            seccomp/user namespaces which are not available inside
        #            unprivileged containers. Without this env var Firefox
        #            crashes silently on launch and no browser window appears.
        #    MOZ_CRASHREPORTER_DISABLE: suppress crash reporter dialogs.
        #    NO_AT_BRIDGE: disable AT-SPI accessibility bridge (can cause
        #            startup delays in headless containers).
        ENV VNC_RESOLUTION=1280x720 \\
            HOME={SANDBOX_HOME} \\
            MOZ_DISABLE_CONTENT_SANDBOX=1 \\
            MOZ_CRASHREPORTER_DISABLE=1 \\
            NO_AT_BRIDGE=1

        # ── Ensure the daytona user's shell is bash (not zsh) ──
        RUN sed -i 's|/usr/bin/zsh|/bin/bash|g' /etc/passwd && \\
            echo "=== /etc/passwd daytona entry ===" && \\
            grep daytona /etc/passwd && \\
            (grep -c '/usr/bin/zsh' /etc/passwd > /dev/null 2>&1 && echo "ERROR: zsh still in /etc/passwd" && exit 1 || true)
        RUN ln -sf /bin/bash /usr/bin/zsh && \\
            echo "=== Shell symlink verification ===" && \\
            ls -la /usr/bin/zsh /bin/bash && \\
            /usr/bin/zsh --version

        # ── Pre-create a Firefox profile so the first-run / import wizard ──
        #    doesn't block the browser from opening the target URL.
        #    The agent launches firefox with --no-remote which uses the default
        #    profile; if none exists Firefox shows a profile-creation dialog
        #    instead of navigating to the URL, and waitForBrowserWindow()
        #    never detects a browser window.
        ENV FIREFOX_PROFILE_DIR={SANDBOX_HOME}/.mozilla/firefox/default
        RUN mkdir -p "$FIREFOX_PROFILE_DIR" && \\
            printf '[General]\\nStartWithLastProfile=1\\n\\n[Profile0]\\nName=default\\nIsRelative=1\\nPath=default\\nDefault=1\\n' \\
                > {SANDBOX_HOME}/.mozilla/firefox/profiles.ini && \\
            printf 'user_pref("browser.shell.checkDefaultBrowser", false);\\nuser_pref("browser.startup.homepage_override.mstone", "ignore");\\nuser_pref("toolkit.telemetry.reportingpolicy.firstRun", false);\\nuser_pref("app.update.enabled", false);\\nuser_pref("browser.rights.3.shown", true);\\nuser_pref("datareporting.policy.dataSubmissionEnabled", false);\\nuser_pref("datareporting.policy.dataSubmissionPolicyNotified", true);\\n' \\
                > "$FIREFOX_PROFILE_DIR/prefs.js" && \\
            chown -R daytona:daytona {SANDBOX_HOME}/.mozilla

        # ── Verify critical packages are installed ──
        RUN echo "=== Verifying Computer Use packages ===" && \\
            for pkg in Xvfb xfce4-session x11vnc novnc firefox-esr xdotool wmctrl; do \\
                which "$pkg" >/dev/null 2>&1 || dpkg -l "$pkg" >/dev/null 2>&1 && echo "  ✓ $pkg" || echo "  ✗ $pkg MISSING"; \\
            done

        USER daytona
        WORKDIR {SANDBOX_HOME}
    """)

    (BUILD_DIR / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    return dockerfile


# ─────────────────────────────────────────────────────────
# Docker build & push
# ─────────────────────────────────────────────────────────
def run_cmd(cmd: list[str], desc: str, check: bool = True) -> subprocess.CompletedProcess:
    print(f"\n{'='*60}")
    print(f"  {desc}")
    print(f"  $ {' '.join(cmd)}")
    print(f"{'='*60}\n")
    result = subprocess.run(cmd, text=True)
    if check and result.returncode != 0:
        print(f"\n✗ FAILED: {desc} (exit code {result.returncode})")
        sys.exit(result.returncode)
    return result


def docker_login(username: str, password: str):
    print("\n🔑 Logging in to Docker Hub...")
    result = subprocess.run(
        ["docker", "login", "--username", username, "--password-stdin"],
        input=password,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"✗ Docker login failed: {result.stderr}")
        sys.exit(1)
    print("✓ Docker login successful")


def docker_build(full_image: str):
    run_cmd(
        ["docker", "build", "--platform", "linux/amd64", "-t", full_image, "-f", str(BUILD_DIR / "Dockerfile"), str(BUILD_DIR)],
        f"Building Docker image: {full_image}",
    )


def docker_push(full_image: str):
    run_cmd(
        ["docker", "push", full_image],
        f"Pushing image to Docker Hub: {full_image}",
    )


# ─────────────────────────────────────────────────────────
# Daytona snapshot creation
# ─────────────────────────────────────────────────────────
def create_daytona_snapshot(api_key: str, snapshot_name: str, full_image: str):
    print(f"\n📦 Creating Daytona snapshot: {snapshot_name}")
    print(f"   Image: {full_image}")
    print(f"   Resources: 2 vCPU, 4 GiB memory, 10 GiB disk\n")

    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "daytona-sdk"], check=True)

        os.environ["DAYTONA_API_KEY"] = api_key
        from daytona import Daytona, CreateSnapshotParams, Resources

        daytona = Daytona()
        snapshot = daytona.snapshot.create(
            CreateSnapshotParams(
                name=snapshot_name,
                image=full_image,
                resources=Resources(cpu=2, memory=4, disk=10),
            ),
            on_logs=lambda chunk: print(chunk, end=""),
        )
        print(f"\n✓ Snapshot created: {snapshot.name}")
        return True
    except Exception as e:
        print(f"\n⚠ Snapshot creation via SDK failed: {e}")
        print("\n📋 Create it manually via the Daytona Dashboard:")
        print(f"   1. Go to https://app.daytona.io/dashboard/snapshots")
        print(f"   2. Click 'Create Snapshot'")
        print(f"   3. Name: {snapshot_name}")
        print(f"   4. Image: {full_image}")
        print(f"   5. Resources: 2 vCPU, 4 GiB memory, 10 GiB disk")
        return False


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  🖥️  Syntera Computer Use Sandbox Image Builder")
    print("=" * 60)
    print("\n  This image bundles the desktop environment stack (Xvfb, xfce4,")
    print("     x11vnc, novnc, Firefox) required by Daytona Computer Use.\n")

    docker_user = get_env("DOCKER_USERNAME", "Docker Hub Username: ")
    docker_pass = get_env("DOCKER_PASSWORD", "Docker Hub Password/Token: ")
    daytona_key = os.environ.get("DAYTONA_API_KEY", "").strip()

    image_name = os.environ.get("IMAGE_NAME", f"{docker_user}/syntera-computer-use-sandbox").strip()
    image_tag = os.environ.get("IMAGE_TAG", "0.1.0").strip()
    snapshot_name = os.environ.get("SNAPSHOT_NAME", "syntera-computer-use-vcpu2-mem4-disk10").strip()
    full_image = f"{image_name}:{image_tag}"

    print(f"\n📋 Configuration:")
    print(f"   Base image:    {BASE_IMAGE}")
    print(f"   New image:     {full_image}")
    print(f"   Snapshot name: {snapshot_name}")
    print(f"   Build dir:     {BUILD_DIR}")

    # Step 1: Generate Dockerfile
    print("\n📄 Step 1: Generating Dockerfile...")
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    dockerfile = generate_dockerfile()
    print("   ✓ Dockerfile created")
    print(f"\n--- Dockerfile ---\n{dockerfile}")

    # Step 2: Docker login
    docker_login(docker_user, docker_pass)

    # Step 3: Build
    print("\n🔨 Step 2: Building Docker image...")
    docker_build(full_image)
    print(f"\n   ✓ Image built: {full_image}")

    # Step 4: Push
    print("\n📤 Step 3: Pushing to Docker Hub...")
    docker_push(full_image)
    print(f"\n   ✓ Image pushed: {full_image}")

    # Step 5: Create Daytona snapshot
    if daytona_key:
        print("\n📦 Step 4: Creating Daytona snapshot...")
        create_daytona_snapshot(daytona_key, snapshot_name, full_image)
    else:
        print("\n📦 Step 4: DAYTONA_API_KEY not set, skipping snapshot creation.")
        print(f"   Create it manually at https://app.daytona.io/dashboard/snapshots")
        print(f"   Name: {snapshot_name}")
        print(f"   Image: {full_image}")
        print(f"   Resources: 2 vCPU, 4 GiB memory, 10 GiB disk")

    # Summary
    print("\n\n" + "=" * 60)
    print("  ✅ BUILD COMPLETE")
    print("=" * 60)
    print(f"\n  Image:    {full_image}")
    print(f"  Snapshot: {snapshot_name}")
    print(f"\n  Update your codebase:")
    print(f"    packages/shared/src/constants.ts:")
    print(f'      DAYTONA_COMPUTER_USE_SNAPSHOT_NAME = "{snapshot_name}"')
    print()


if __name__ == "__main__":
    main()
