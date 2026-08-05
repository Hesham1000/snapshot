#!/usr/bin/env python3
"""
Build a pre-configured Daytona sandbox Docker image for authorized web/WordPress
penetration testing engagements.

This script:
1. Generates all pentest-toolkit template files (README, requirements.txt,
   loot/reports scaffolding, helper scripts)
2. Builds a Docker image extending daytonaio/langchain-open-swe:0.1.0
3. Pre-installs the pinned Python toolkit (sqlmap, wapiti3, dirsearch, sslyze,
   shodan, etc.) plus external non-pip tools (Nmap, ffuf, gobuster, WPScan,
   theHarvester)
4. Pushes to Docker Hub
5. Creates a Daytona snapshot via the Python SDK

Usage (on GitHub Codespaces or any Linux machine with Docker):
    export DOCKER_USERNAME=yourusername
    export DOCKER_PASSWORD=dckr_pat_xxx
    export DAYTONA_API_KEY=dtn_xxx
    python3 build_pentest_sandbox_image.py

Or run directly and you'll be prompted for any missing env vars.

IMPORTANT: This image bundles active-scanning / exploitation tooling
(sqlmap, wapiti, wpscan, ffuf, gobuster, nmap, etc.). It must only ever be
used by agents operating under a signed, in-scope authorization (ROE) for
the target being tested. See the generated README.md (section "Authorization
& Scope") that ships inside the image for the operating rules agents must
follow.
"""

import json
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
SANDBOX_TEMPLATE_SUBDIR = "_template"
SANDBOX_TEMPLATE_PATH = f"{SANDBOX_HOME}/{SANDBOX_TEMPLATE_SUBDIR}"

BUILD_DIR = Path("/tmp/syntera-pentest-sandbox-build")
TEMPLATE_DIR = BUILD_DIR / "template"

# Pinned Python requirements, verified working together (see README section 1
# "Important version pins" for why setuptools/bcrypt are pinned).
PYTHON_REQUIREMENTS = """\
aiocache==0.12.2
aiohappyeyeballs==2.7.1
aiohttp==3.10.11
aioquic==0.9.25
aioresponses==0.7.9
aiosignal==1.4.0
aiosqlite==0.20.0
annotated-types==0.8.0
anyio==4.14.2
asgiref==3.7.2
attrs==26.1.0
bcrypt==4.0.1
beautifulsoup4==4.12.3
blinker==1.9.0
Brotli==1.1.0
browser-cookie3==0.19.1
certifi==2026.7.22
cffi==2.1.0
chardet==7.4.3
charset-normalizer==2.0.12
click==8.4.2
click-plugins==1.1.1.2
colorama==0.4.6
cryptography==49.0.0
defusedxml==0.7.1
dirsearch==0.4.3.post1
dnspython==2.6.1
filelock==3.32.0
Flask==3.0.3
frozenlist==1.8.0
greenlet==3.5.4
h11==0.14.0
h2==4.4.0
hpack==4.2.0
httpcore==1.0.4
httpretty==1.1.4
httpx==0.27.0
httpx-ntlm==1.4.0
humanize==4.9.0
hyperframe==6.1.0
idna==3.18
invoke==3.0.3
itsdangerous==2.2.0
Jinja2==3.1.6
kaitaistruct==0.10
ldap3==2.9.1
loguru==0.7.2
lxml==6.1.1
lz4==4.4.5
Mako==1.3.2
markdown-it-py==4.2.0
MarkupSafe==2.1.5
mdurl==0.1.2
mitmproxy==10.2.3
msgpack==1.0.8
multidict==6.7.1
nassl==5.4.0
ntlm-auth==1.5.0
packaging==24.1
paramiko==5.0.0
passlib==1.7.4
propcache==0.5.2
protobuf==4.25.9
publicsuffix2==2.20191221
pyasn1==0.5.1
pyasn1_modules==0.4.1
pycparser==3.0
pycryptodomex==3.23.0
pydantic==2.13.4
pydantic_core==2.46.4
pydivert==2.1.0
Pygments==2.20.0
PyJWT==2.13.0
pylsqpack==0.3.24
PyNaCl==1.6.2
pyOpenSSL==26.3.0
pyparsing==3.1.4
pyperclip==1.8.2
PySocks==1.7.1
pyspnego==0.12.1
python-nmap==0.7.1
python-Wappalyzer==0.3.1
python-wordpress-xmlrpc==2.3
PyYAML==6.0.3
requests==2.34.2
requests-file==3.0.1
requests_ntlm==1.3.0
rich==15.0.0
ruamel.yaml==0.18.17
ruamel.yaml.clib==0.2.15
service-identity==24.2.0
shodan==1.31.0
sniffio==1.3.1
socksio==1.0.0
sortedcontainers==2.4.0
soupsieve==2.9.1
SQLAlchemy==2.0.28
sqlmap==1.10.7
sslyze==6.3.1
sspilib==0.5.0
structlog==24.4.0
tabulate==0.10.0
tld==0.13
tldextract==5.3.1
tls_parser==2.0.2
tqdm==4.70.0
typing-inspection==0.4.2
typing_extensions==4.16.0
urllib3==2.7.0
wapiti-swagger==0.1.9
wapiti3==3.2.3
wapiti_arsenic==28.2
Werkzeug==3.1.8
wsproto==1.2.0
xlsxwriter==3.2.9
yarl==1.24.5
yaswfp==0.9.3
zstandard==0.22.0
"""
# NOTE: Windows-only packages from the original desktop venv are intentionally
# dropped for the Linux container: nassl/pywin32-style deps that don't apply,
# plus mitmproxy-windows, mitmproxy_rs (platform-specific wheel; mitmproxy on
# Linux pulls its own rs backend), urwid-mitmproxy, win32_setctime. If any
# tool import fails at build time because of this, check the build logs and
# re-add the specific package.

# Go-based tools built from source inside the image (pinned versions).
FFUF_VERSION = "v2.1.0"
GOBUSTER_VERSION = "v3.6.0"
NUCLEI_VERSION = "v3.3.7"
SUBFINDER_VERSION = "v2.6.7"
AMASS_VERSION = "v4.2.0"
GO_VERSION = "1.23.4"  # Debian bookworm's apt `golang-go` is 1.19 — too old
                       # to parse the "go 1.21.0"-style three-part version
                       # directive nuclei/subfinder's go.mod files use.

# External tool versions
NMAP_MIN = "nmap"  # apt package, version tracks distro
THEHARVESTER_REPO = "https://github.com/laramies/theHarvester.git"
TESTSSL_REPO = "https://github.com/drwetter/testssl.sh.git"
NIKTO_REPO = "https://github.com/sullo/nikto.git"
XSSTRIKE_REPO = "https://github.com/s0md3v/XSStrike.git"
JWT_TOOL_REPO = "https://github.com/ticarpi/jwt_tool.git"

# Additional pip-installed pentest libraries/CLIs (installed unpinned-latest
# in a separate layer from the core pinned toolkit, since impacket/scapy/
# netexec pull their own dependency trees that can conflict with the
# wapiti/sslyze pins above — keeping them in a separate `pip install` layer
# means a version bump here can't silently break the core toolkit's pins).
EXTRA_PIP_PACKAGES = [
    "impacket",        # AD/SMB/Kerberos protocol library + CLI scripts (secretsdump.py, etc.)
    "scapy",           # Packet crafting / manipulation
    "netexec",         # Actively maintained fork of crackmapexec (AD/SMB lateral movement). Binary: `nxc`
    "arjun",           # HTTP parameter discovery/fuzzing
    "graphql-cop",     # GraphQL security scanner (introspection, batching, CSRF, etc.)
]


def get_env(key: str, prompt: str = None) -> str:
    """Get env var or prompt interactively."""
    val = os.environ.get(key, "").strip()
    if not val:
        if prompt:
            val = input(prompt).strip()
        if not val:
            print(f"✗ {key} is required")
            sys.exit(1)
    return val


# ─────────────────────────────────────────────────────────
# Template file generation
# ─────────────────────────────────────────────────────────
def write_template(rel_path: str, content: str):
    """Write a file into the template directory."""
    fp = TEMPLATE_DIR / rel_path
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")


def generate_requirements_file():
    write_template("requirements.txt", PYTHON_REQUIREMENTS)


def generate_loot_scaffolding():
    """Create the loot/ and reports/ directory structure the README documents."""
    for sub in [
        "loot/.gitkeep",
        "loot/README.md",
        "reports/.gitkeep",
    ]:
        pass

    write_template("loot/.gitkeep", "")
    write_template("reports/.gitkeep", "")
    write_template("loot/README.md", textwrap.dedent("""\
        # loot/

        Raw tool output goes here, organized per engagement:

        ```
        loot/
          <target>/
            recon/        # nmap, dns, shodan, wappalyzer output
            enum/         # dirsearch, content discovery
            scan/         # wapiti, sslyze, nikto output
            sqli/         # sqlmap output
            wp/           # wordpress-specific findings
        ```

        Name files `<tool>-<timestamp>.<ext>` (e.g. `wapiti-20260803.html`).
        Never commit real target data from this folder outside the engagement's
        authorized storage location.
    """))
    write_template("reports/README.md", textwrap.dedent("""\
        # reports/

        Final markdown/html reports per engagement, built from `loot/` findings
        using `rich`/`tabulate`. See the top-level README.md, section 7, for the
        expected report structure (executive summary, scope, findings table,
        raw output references).
    """))


def generate_agent_readme():
    """Sandbox-adapted version of the agent operating guide."""
    write_template("README.md", textwrap.dedent("""\
        # Penetration Testing Sandbox - AI Agent Guide

        A pre-configured environment containing the most-used tools and libraries
        for **web application** and **WordPress** penetration testing, baked into
        this Daytona sandbox image.

        This document is the reference for AI agents assisting with authorized
        penetration testing engagements. Read it before running any tooling.

        ---

        ## 0. Authorization & Scope (READ FIRST)

        > Only use this toolkit against systems you are **explicitly authorized**
        > to test. Every command below assumes a valid, signed scope (Rules of
        > Engagement / ROE) is in place.

        Before acting, an AI agent MUST:

        1. Confirm the target is within the agreed scope (in-scope hosts, IP
           ranges, URL paths, and excluded assets).
        2. Respect rate limits, maintenance windows, and depth constraints from
           the statement of work.
        3. Avoid destructive techniques (DoS, mass data exfiltration, destructive
           payloads) unless explicitly permitted.
        4. Log all activity (commands, timestamps, findings) under `./loot/` or
           `./reports/` for the engagement.
        5. Never store or print secrets (credentials, API keys, session tokens)
           in plaintext logs unless the engagement requires it; prefer
           masked/redacted output.

        If scope is unclear, STOP and ask the operator.

        ---

        ## 1. Environment

        Unlike the original desktop venv setup, this sandbox image installs all
        tooling directly into the container's system Python — the container
        itself is the isolation boundary, so no `.venv` is needed. Everything
        under section 2 is already on `PATH` / importable at sandbox start.

        > **Important version pins baked into this image (do not upgrade
        > blindly):**
        > - `setuptools==80.9.0` - setuptools >=81 removes `pkg_resources`,
        >   which `dirsearch` and `python-Wappalyzer` import at startup.
        > - `bcrypt==4.0.1` - required for compatibility with `passlib` (used
        >   by `mitmproxy`, a dependency of `wapiti3`). bcrypt >=4.1 / 5.x
        >   breaks `wapiti` on startup.
        > - `wapiti3` pins older `cryptography`/`httpx`/`mitmproxy`; `sslyze`
        >   and `pyOpenSSL` use newer `cryptography`. Dependency-conflict
        >   warnings during the image build are expected; the pinned set in
        >   `requirements.txt` is verified working.

        ---

        ## 2. Installed Tools - Quick Reference

        ### CLI tools

        | Tool | Command | Purpose |
        |------|---------|---------|
        | sqlmap | `sqlmap` | Automatic SQL injection detection & exploitation |
        | dirsearch | `dirsearch` | Directory/file & path brute-forcing |
        | wapiti | `wapiti` / `wapiti-getcookie` | Black-box web vulnerability scanner |
        | sslyze | `sslyze` | SSL/TLS configuration & vulnerability scanner |
        | shodan | `shodan` | Shodan internet-exposed-device search (needs API key) |
        | httpx | `httpx` | HTTP client CLI (quick request inspection) |
        | mitmproxy | `mitmproxy` / `mitmdump` / `mitmweb` | Intercepting proxy |
        | nmap | `nmap` | Port/service scanning (apt package, baked into image) |
        | ffuf | `ffuf` | Fast content/parameter/vhost fuzzer (built from source) |
        | gobuster | `gobuster` | Directory & vhost brute-forcing (built from source) |
        | wpscan | `wpscan` | WordPress plugin/theme/user/CVE scanner (Ruby gem) |
        | theHarvester | `theHarvester` | OSINT: emails, subdomains, hosts (git clone, not the stub PyPI package) |
        | nikto | `nikto` | Web server misconfiguration & known-vuln scanner |
        | whatweb | `whatweb` | Web technology fingerprinting |
        | nuclei | `nuclei` | Template-driven vulnerability scanner (CVEs, misconfig, exposed panels) |
        | subfinder | `subfinder` | Passive subdomain enumeration |
        | amass | `amass` | Active/passive subdomain enum & attack-surface mapping |
        | testssl.sh | `testssl.sh` | Deep TLS/SSL configuration & vulnerability scanner |
        | hydra | `hydra` | Online login brute-forcing (SSH, HTTP forms, FTP, etc.) |
        | john | `john` | Offline password hash cracking |
        | xsstrike | `xsstrike` | Reflected/DOM XSS detection & payload fuzzing |
        | jwt_tool | `jwt_tool` | JWT vulnerability testing (alg confusion, weak secrets, claim tampering) |
        | netexec | `nxc` | AD/SMB/WinRM lateral-movement & credential validation (actively maintained crackmapexec fork) |
        | arjun | `arjun` | HTTP parameter discovery/fuzzing |
        | graphql-cop | `graphql-cop` | GraphQL security scanner (introspection, batching, CSRF) |

        ### Python libraries (import from scripts)

        | Library | Import | Purpose |
        |---------|--------|---------|
        | requests | `import requests` | Synchronous HTTP client |
        | httpx | `import httpx` | Sync + async HTTP/2 client |
        | BeautifulSoup4 | `from bs4 import BeautifulSoup` | HTML/XML parsing & scraping |
        | lxml | `from lxml import html, etree` | Fast HTML/XML parsing, XPath |
        | urllib3 | `import urllib3` | Low-level HTTP, connection pooling |
        | python-nmap | `import nmap` | Programmatic Nmap wrapper (Nmap binary baked in) |
        | python-Wappalyzer | `from Wappalyzer import Wappalyzer, WebPage` | Tech-stack fingerprinting |
        | PyJWT | `import jwt` | JWT encode/decode/analyze |
        | pyOpenSSL | `from OpenSSL import crypto, SSL` | TLS cert & SSL inspection |
        | cryptography | `from cryptography import x509` | Crypto primitives, cert parsing |
        | dnspython | `import dns.resolver` | DNS queries & enumeration |
        | tldextract | `import tldextract` | Robust subdomain/domain parsing |
        | paramiko | `import paramiko` | SSH client (post-exploitation, server checks) |
        | shodan | `import shodan` | Shodan API client |
        | sslyze | `from sslyze import ...` | Scriptable TLS scanning |
        | python-wordpress-xmlrpc | `from wordpress_xmlrpc import Client` | WordPress XML-RPC interaction |
        | PySocks | `import socks` | SOCKS proxy support |
        | rich | `from rich import ...` | Pretty terminal output / reporting |
        | tabulate | `from tabulate import tabulate` | Tables for reports |
        | impacket | `from impacket.smbconnection import SMBConnection` etc. | AD/SMB/Kerberos protocol library; also ships CLI scripts (`secretsdump.py`, `GetNPUsers.py`, ...) at `/usr/local/bin` |
        | scapy | `from scapy.all import *` | Raw packet crafting & manipulation |

        > **Note on scope for AD/network tools** (impacket, netexec, hydra,
        > john, scapy): these are included for engagements whose scope
        > explicitly covers internal network/Active Directory testing. For a
        > pure web-app/WordPress engagement, they should stay unused —
        > authorization is still checked per section 0, tool-by-tool, not
        > just per-image.

        ---

        ## 3. Engagement Workflow

        A typical web/WordPress assessment follows these phases. Save outputs
        under `./loot/<target>/` and `./reports/`.

        ```
        loot/
          <target>/
            recon/        # nmap, dns, shodan, wappalyzer output
            enum/         # dirsearch, content discovery
            scan/         # wapiti, sslyze, nikto output
            sqli/         # sqlmap output
            wp/           # wordpress-specific findings
        reports/
          <target>/       # final markdown/html reports
        ```

        ### Phase 1 - Reconnaissance & Fingerprinting
        Technology fingerprinting (Wappalyzer), DNS enumeration (dnspython),
        subdomain parsing (tldextract), Shodan lookups.

        ### Phase 2 - Port & Service Discovery
        `nmap` CLI or the `python-nmap` wrapper.

        ### Phase 3 - Content & Directory Enumeration
        `dirsearch`, `ffuf`, or `gobuster` for directories/files/vhosts.

        ### Phase 4 - Vulnerability Scanning
        `wapiti` for black-box scanning (XSS, SQLi, SSRF, XXE), `sqlmap` for
        SQL injection, `sslyze` for TLS configuration issues.

        ### Phase 5 - WordPress-Specific Testing
        `wpscan` is the primary WordPress scanner (plugins, themes, users,
        CVEs). Combine with manual checks of `/wp-json/wp/v2/users`,
        `/readme.html`, XML-RPC, and REST API exposure.

        ---

        ## 4. Proxies & Logging

        Route tool traffic through mitmproxy for inspection and logging:

        ```bash
        mitmweb --listen-port 8080
        mitmdump -w loot/<target>/flows.mitm
        ```

        Point tools at the proxy with `--proxy`, `--proxy=`, or the
        equivalent flag for each tool.

        ---

        ## 5. Output & Reporting Conventions

        - Save all raw tool output under `loot/<target>/<phase>/`.
        - Name files `<tool>-<timestamp>.<ext>`.
        - Build the final report in `reports/<target>/` using `rich`/`tabulate`:
          executive summary, scope & methodology, findings table (ID, severity,
          title, affected URL, evidence, remediation), raw output references.
        - Severity scale: Critical / High / Medium / Low / Informational.

        ---

        ## 6. Agent Operating Rules

        1. **Confirm scope** before any active scan (section 0).
        2. **Prefer library calls** for repeatability; use CLI for one-off scans.
        3. **Throttle** aggressive scans (`-t` / `--threads` / rate flags) to
           avoid DoS.
        4. **Save evidence** to `loot/`; never inline secrets in chat output.
        5. **Cite tool + command** for every finding so it is reproducible.
        6. If a tool errors with a missing binary, note it and fall back to an
           installed equivalent, or ask the operator to add it.

        ---

        ## 7. Key Tool Versions (verified working)

        See `requirements.txt` for the full pinned Python environment, and the
        Dockerfile used to build this image for external tool versions
        (ffuf, gobuster, wpscan, theHarvester, nmap).
    """))


def generate_helper_scripts():
    """Wrapper scripts for common recon steps, callable by the agent."""
    write_template("scripts/new-engagement.sh", textwrap.dedent("""\
        #!/bin/bash
        # Scaffold loot/ and reports/ folders for a new authorized engagement.
        set -euo pipefail
        TARGET="${1:?Usage: new-engagement.sh <target-name>}"
        mkdir -p "loot/${TARGET}"/{recon,enum,scan,sqli,wp}
        mkdir -p "reports/${TARGET}"
        echo "Scaffolded loot/${TARGET} and reports/${TARGET}"
        echo "Reminder: confirm signed ROE / scope before any active scanning."
    """))

    write_template("scripts/check-tools.sh", textwrap.dedent("""\
        #!/bin/bash
        # Sanity-check that all baked-in tools are importable/callable.
        set -uo pipefail
        echo "== CLI tools =="
        for tool in sqlmap dirsearch wapiti sslyze shodan httpx mitmproxy nmap ffuf gobuster wpscan theHarvester \\
                    nikto whatweb nuclei subfinder amass testssl.sh hydra john xsstrike jwt_tool nxc arjun graphql-cop; do
          if command -v "$tool" >/dev/null 2>&1; then
            echo "  ✓ $tool"
          else
            echo "  ✗ $tool (missing from PATH)"
          fi
        done

        echo "== Python libraries =="
        python3 - <<'PYEOF'
import importlib
mods = [
    "requests", "httpx", "bs4", "lxml", "urllib3", "nmap", "Wappalyzer",
    "jwt", "OpenSSL", "cryptography", "dns.resolver", "tldextract",
    "paramiko", "shodan", "sslyze", "wordpress_xmlrpc", "socks", "rich",
    "tabulate", "impacket", "scapy",
]
for m in mods:
    try:
        importlib.import_module(m)
        print(f"  ✓ {m}")
    except Exception as e:
        print(f"  ✗ {m} ({e})")
PYEOF
    """))


def generate_gitignore():
    write_template(".gitignore", textwrap.dedent("""\
        # Engagement data — never commit real target findings
        loot/*/
        reports/*/
        !loot/.gitkeep
        !reports/.gitkeep
        *.mitm
        __pycache__/
        *.pyc
        .env
    """))


# ─────────────────────────────────────────────────────────
# Dockerfile generation
# ─────────────────────────────────────────────────────────
def generate_dockerfile():
    """Create the Dockerfile that bakes everything into the image."""

    dockerfile = textwrap.dedent(f"""\
        FROM {BASE_IMAGE}

        USER root

        ENV DEBIAN_FRONTEND=noninteractive

        # The base image ships a leftover Yarn apt source (dl.yarnpkg.com) whose
        # GPG key isn't installed, which makes `apt-get update` hard-fail on
        # Debian bookworm (unsigned repo = error, not just a warning). Node/Yarn
        # are already installed in the base image, so the repo isn't needed —
        # just drop it before touching apt.
        RUN find /etc/apt/sources.list.d/ -type f -exec grep -l 'yarnpkg' {{}} \\; 2>/dev/null | xargs -r rm -f

        # ── System packages: Nmap, Go (for ffuf/gobuster), Ruby (for wpscan),
        #    git, and the headers a few Python C-extensions need ──
        RUN apt-get update && apt-get install -y --no-install-recommends \\
                zsh \\
                nmap \\
                whois \\
                dnsutils \\
                git \\
                curl \\
                build-essential \\
                libssl-dev \\
                libffi-dev \\
                libpcap-dev \\
                python3-dev \\
                ruby-full \\
                rubygems \\
                ca-certificates \\
                perl \\
                libnet-ssleay-perl \\
                whatweb \\
                hydra \\
                john \\
                smbclient \\
            && rm -rf /var/lib/apt/lists/*

        # ── Modern upstream Go toolchain. Debian bookworm's apt `golang-go`
        #    is 1.19, which can't parse the "go 1.21.0"-style three-part
        #    version directive that nuclei/subfinder/amass's go.mod files
        #    declare — installing straight from go.dev avoids that entirely. ──
        RUN curl -fsSL https://go.dev/dl/go{GO_VERSION}.linux-amd64.tar.gz -o /tmp/go.tar.gz && \\
            tar -C /usr/local -xzf /tmp/go.tar.gz && \\
            rm /tmp/go.tar.gz
        ENV PATH="/usr/local/go/bin:${{PATH}}"

        # ── ffuf (built from source, pinned version) ──
        RUN GOBIN=/usr/local/bin go install github.com/ffuf/ffuf/v2@{FFUF_VERSION}

        # ── gobuster (built from source, pinned version) ──
        RUN GOBIN=/usr/local/bin go install github.com/OJ/gobuster/v3@{GOBUSTER_VERSION}

        # ── nuclei (template-driven vulnerability scanner, pinned version) ──
        RUN GOBIN=/usr/local/bin go install github.com/projectdiscovery/nuclei/v3/cmd/nuclei@{NUCLEI_VERSION}

        # ── subfinder (passive subdomain enumeration, pinned version) ──
        RUN GOBIN=/usr/local/bin go install github.com/projectdiscovery/subfinder/v2/cmd/subfinder@{SUBFINDER_VERSION}

        # ── amass (active/passive subdomain + attack-surface mapping, pinned version) ──
        RUN GOBIN=/usr/local/bin go install github.com/owasp-amass/amass/v4/...@{AMASS_VERSION}

        # ── wpscan (Ruby gem — the primary WordPress scanner) ──
        RUN gem install wpscan --no-document

        # ── testssl.sh (TLS/SSL configuration scanner, bash-based) ──
        RUN git clone --depth 1 {TESTSSL_REPO} /opt/testssl.sh && \\
            ln -sf /opt/testssl.sh/testssl.sh /usr/local/bin/testssl.sh && \\
            chmod +x /usr/local/bin/testssl.sh

        # ── nikto (Perl web server scanner — lives in Debian's non-free
        #    component, which isn't reliably enabled across base images, so
        #    it's git-cloned instead of apt-installed) ──
        RUN git clone --depth 1 {NIKTO_REPO} /opt/nikto && \\
            printf '#!/bin/bash\\nperl /opt/nikto/program/nikto.pl "$@"\\n' > /usr/local/bin/nikto && \\
            chmod +x /usr/local/bin/nikto

        # Place all template files inside _template/ subdirectory.
        # At sandbox init time, this gets renamed to the project name via a
        # single O(1) mv instead of moving thousands of files individually.
        WORKDIR {SANDBOX_TEMPLATE_PATH}

        # Copy all template files into the _template subdirectory
        COPY template/ {SANDBOX_TEMPLATE_PATH}/

        # ── theHarvester (GitHub repo — the PyPI package is a stub).
        #    `pip install .` reads pyproject.toml and also registers a
        #    `theHarvester` console-script entry point on PATH, so no
        #    manual symlink/chmod is needed (and the old root-level
        #    theHarvester.py path this used to point to no longer exists —
        #    the executable now lives inside the nested package dir). ──
        RUN git clone --depth 1 {THEHARVESTER_REPO} /opt/theHarvester && \\
            pip install --no-cache-dir --break-system-packages /opt/theHarvester 2>&1 | tail -20

        # ── XSStrike (reflected/DOM XSS detection + fuzzing) ──
        RUN git clone --depth 1 {XSSTRIKE_REPO} /opt/XSStrike && \\
            pip install --no-cache-dir --break-system-packages -r /opt/XSStrike/requirements.txt 2>&1 | tail -20 && \\
            printf '#!/bin/bash\\npython3 /opt/XSStrike/xsstrike.py "$@"\\n' > /usr/local/bin/xsstrike && \\
            chmod +x /usr/local/bin/xsstrike

        # ── jwt_tool (JWT vulnerability testing: alg confusion, weak secrets, etc.) ──
        RUN git clone --depth 1 {JWT_TOOL_REPO} /opt/jwt_tool && \\
            pip install --no-cache-dir --break-system-packages -r /opt/jwt_tool/requirements.txt 2>&1 | tail -20 && \\
            printf '#!/bin/bash\\npython3 /opt/jwt_tool/jwt_tool.py "$@"\\n' > /usr/local/bin/jwt_tool && \\
            chmod +x /usr/local/bin/jwt_tool

        # ── Pinned Python toolkit (setuptools/wheel first, per version-pin notes) ──
        RUN pip install --no-cache-dir --break-system-packages --upgrade pip setuptools==80.9.0 wheel && \\
            cd {SANDBOX_TEMPLATE_PATH} && \\
            pip install --no-cache-dir --break-system-packages -r requirements.txt 2>&1 | tail -30

        # ── Extra pentest libraries/CLIs (separate layer, unpinned-latest — see
        #    EXTRA_PIP_PACKAGES comment for why these are isolated from the pins
        #    above). netexec/impacket build native extensions, hence the earlier
        #    build-essential/libssl-dev/libffi-dev/libpcap-dev apt packages. ──
        RUN pip install --no-cache-dir --break-system-packages {' '.join(EXTRA_PIP_PACKAGES)} 2>&1 | tail -30

        RUN chmod +x {SANDBOX_TEMPLATE_PATH}/scripts/*.sh

        # Git init + ownership (single layer)
        RUN cd {SANDBOX_TEMPLATE_PATH} && \\
            git config --global user.email "agent@syntera.ai" && \\
            git config --global user.name "Syntera Agent" && \\
            git config --global init.defaultBranch main && \\
            git init && \\
            git add -A && \\
            git commit -m "Initial pentest sandbox template" --allow-empty 2>/dev/null || true && \\
            chown -R daytona:daytona {SANDBOX_TEMPLATE_PATH}

        USER daytona
        WORKDIR {SANDBOX_HOME}
    """)

    (BUILD_DIR / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    return dockerfile


# ─────────────────────────────────────────────────────────
# Docker build & push
# ─────────────────────────────────────────────────────────
def run_cmd(cmd: list[str], desc: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command with live output."""
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
    """Create a Daytona snapshot using the Python SDK."""
    print(f"\n📦 Creating Daytona snapshot: {snapshot_name}")
    print(f"   Image: {full_image}")
    print(f"   Resources: 2 vCPU, 4 GiB memory, 10 GiB disk\n")

    try:
        # Install the SDK if not present
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
    print("  🔒 Syntera Pentest Sandbox Image Builder")
    print("=" * 60)
    print("\n  ⚠  This image bundles active-scanning tooling (sqlmap, wapiti,")
    print("     wpscan, ffuf, gobuster, nmap, ...). Only use it against")
    print("     targets you are explicitly authorized to test.\n")

    # Gather credentials
    docker_user = get_env("DOCKER_USERNAME", "Docker Hub Username: ")
    docker_pass = get_env("DOCKER_PASSWORD", "Docker Hub Password/Token: ")
    daytona_key = os.environ.get("DAYTONA_API_KEY", "").strip()

    image_name = os.environ.get("IMAGE_NAME", f"{docker_user}/syntera-pentest-sandbox").strip()
    image_tag = os.environ.get("IMAGE_TAG", "0.1.0").strip()
    snapshot_name = os.environ.get("SNAPSHOT_NAME", "syntera-pentest-vcpu2-mem4-disk10").strip()
    full_image = f"{image_name}:{image_tag}"

    print(f"\n📋 Configuration:")
    print(f"   Base image:    {BASE_IMAGE}")
    print(f"   New image:     {full_image}")
    print(f"   Snapshot name: {snapshot_name}")
    print(f"   Build dir:     {BUILD_DIR}")

    # Step 1: Clean & generate template files
    print("\n\n📁 Step 1: Generating template files...")
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)

    generate_requirements_file()
    generate_loot_scaffolding()
    generate_agent_readme()
    generate_helper_scripts()
    generate_gitignore()

    file_count = sum(1 for _ in TEMPLATE_DIR.rglob("*") if _.is_file())
    print(f"   ✓ Generated {file_count} template files")

    # Step 2: Generate Dockerfile
    print("\n📄 Step 2: Generating Dockerfile...")
    dockerfile = generate_dockerfile()
    print("   ✓ Dockerfile created")
    print(f"\n--- Dockerfile ---\n{dockerfile}")

    # Step 3: Docker login
    docker_login(docker_user, docker_pass)

    # Step 4: Build
    print("\n🔨 Step 3: Building Docker image (this may take 20-35 minutes — the five")
    print("           Go tool builds, three git-clone toolchains, and pip installs")
    print("           are the slow parts)...")
    docker_build(full_image)
    print(f"\n   ✓ Image built: {full_image}")

    # Step 5: Push
    print("\n📤 Step 4: Pushing to Docker Hub...")
    docker_push(full_image)
    print(f"\n   ✓ Image pushed: {full_image}")

    # Step 6: Create Daytona snapshot
    if daytona_key:
        print("\n📦 Step 5: Creating Daytona snapshot...")
        create_daytona_snapshot(daytona_key, snapshot_name, full_image)
    else:
        print("\n📦 Step 5: DAYTONA_API_KEY not set, skipping snapshot creation.")
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
    print(f'      DAYTONA_PENTEST_IMAGE_NAME = "{full_image}"')
    print(f'      DAYTONA_PENTEST_SNAPSHOT_NAME = "{snapshot_name}"')
    print(f"\n    apps/open-swe/src/constants.ts:")
    print(f"      pentest.setup_script can be simplified to a no-op / smoke")
    print(f"      test (run scripts/check-tools.sh) since the toolkit and")
    print(f"      external binaries are pre-baked in the image.\n")


if __name__ == "__main__":
    main()