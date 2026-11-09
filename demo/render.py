"""Render the listwarden narrated demo: real command output + edge-tts narration.

Usage: python demo/render.py   (needs ffmpeg, edge-tts, Pillow, network)
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(HERE, "build")
OUT = os.path.join(ROOT, "demo.mp4")
SDKBIN = "/tmp/lwvenv/bin"
SANDBOX = "/tmp/lwdemo"
W, H, FPS = 1280, 720, 24
VOICE = "en-US-AndrewNeural"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BG, FG = (24, 26, 33), (220, 223, 228)
ANSI = {"31": (255, 95, 95), "32": (95, 215, 120), "33": (240, 200, 80),
        "2": (130, 135, 145), "36": (90, 200, 230), "35": (200, 140, 230), "1": FG}
PROMPT = "dev@laptop ~/lists $ "

SCENES = [
    {
        "kind": "title",
        "title": "listwarden",
        "sub": "keep awesome lists awesome:\nlink rot audits + PR link-blast checks",
        "say": "This is list warden, a small open source tool for people who maintain awesome lists and other "
               "curated link lists on GitHub.",
    },
    {
        "kind": "bullets",
        "title": "The problem",
        "bullets": [
            "\"Awesome repositories are no longer awesome\" (HN, Nov 2026)",
            "Link rot: archived, renamed, deleted and abandoned entries",
            "   awesome-R: 486 entries, 65 renamed repos, 11 archived",
            "PR floods: one account sent 100+ PRs to awesome lists",
            "   for its own vibe-coded sites. Vetting each one is manual.",
        ],
        "say": "A post on Hacker News last week argued that awesome lists are no longer awesome, for two reasons. "
               "Link rot: entries get archived, renamed, deleted or abandoned, and nobody notices. "
               "And pull request floods: the author found one account that had sent over a hundred pull requests "
               "to awesome lists, all for its own sites. Checking each one by hand costs maintainers real time.",
    },
    {
        "kind": "term",
        "cmds": ["listwarden audit qinwf/awesome-R --no-web --limit 14"],
        "say": "list warden audit reads a list, here awesome R straight from GitHub, and looks up every repo "
               "in a few GraphQL calls. It finds deleted repos, duplicates, archived projects, renamed repos and "
               "anything with no commits in two years. Only seventy nine of the two hundred and five repo entries "
               "come back clean, and sixty five are fixable automatically.",
    },
    {
        "kind": "term",
        "cmds": ["listwarden fix awesome-R.md --no-web | grep -E '^[-+][*-] ' | grep -v emoji | head -8 | cut -c1-110",
                 "listwarden fix awesome-R.md --no-web --prune dead,archived --write | tail -1"],
        "say": "list warden fix is a dry run by default. It rewrites renamed repos and permanent redirects to "
               "their new URLs, and touches nothing else. With prune, it also drops dead or archived entries, "
               "and with write it saves the file, ready for a commit.",
    },
    {
        "kind": "term",
        "cmds": ["listwarden audit widgets.md"],
        "env": {"LISTWARDEN_CASSETTE": "CASSETTE", "LISTWARDEN_NOW": "2026-11-09T12:00:00Z"},
        "say": "Regular web links get checked too: dead pages, moved sites, and pages that block bots, which it "
               "reports as unverified instead of dead. This part is a recorded example with made up names, so it "
               "runs the same every time.",
    },
    {
        "kind": "term",
        "cmds": ["listwarden pr lists/awesome-widgets 42"],
        "env": {"LISTWARDEN_CASSETTE": "CASSETTE", "LISTWARDEN_NOW": "2026-11-09T12:00:00Z"},
        "say": "The second half is for pull requests. list warden pr reads only the lines the PR adds and "
               "prints facts for each new link: stars, repo age, commits, contributors, whether it's already listed, "
               "and whether the submitter owns it. Then it looks at the submitter: how many other awesome lists they "
               "sent pull requests to this week, and how many times they pitched the same link. It reports facts, "
               "not a verdict.",
    },
    {
        "kind": "bullets",
        "title": "As a GitHub Action",
        "bullets": [
            "uses: hamarshehmhmd/listwarden@v0.1.0",
            "on pull_request_target: one sticky comment with the facts",
            "   reads the PR via the API, never runs PR code",
            "on schedule: weekly audit in the job summary",
            "Thresholds: --min-stars, --min-age-days, --blast-threshold",
        ],
        "say": "As a GitHub Action, it posts one sticky comment on each pull request, and a scheduled run audits "
               "the whole list every week into the job summary. Every threshold is a flag.",
    },
    {
        "kind": "bullets",
        "title": "Limits",
        "bullets": [
            "Facts, not judgment: a new repo can still be great",
            "Some sites block bots: those are reported, not called dead",
            "Search API counts are capped and rate-limited",
            "GitHub links only get the deep checks",
        ],
        "say": "The limits. It doesn't judge quality, a brand new project can still be great. Some sites block "
               "automated checks, so those get flagged for a human. GitHub search counts are rate limited. "
               "And deep checks only work for GitHub repos.",
    },
    {
        "kind": "bullets",
        "title": "Try it",
        "bullets": [
            "curl -fsSL https://raw.githubusercontent.com/",
            "   hamarshehmhmd/listwarden/main/install.sh | sh",
            "listwarden audit   ·   listwarden fix   ·   listwarden pr",
            "github.com/hamarshehmhmd/listwarden",
            "GNU AGPL-3.0-or-later (commercial licenses available)",
        ],
        "say": "Install it with the one line installer. It needs only Python, with no dependencies. It's licensed "
               "under the GNU Affero General Public License version 3 or later, with commercial licenses available "
               "from the author. It's on GitHub at hamarsheh m h m d slash list warden.",
    },
]


async def tts(text, path):
    import edge_tts
    for attempt in range(8):
        try:
            await edge_tts.Communicate(text, VOICE, rate="+4%").save(path)
            return
        except Exception:
            if os.path.exists(path):
                os.remove(path)
            if attempt == 7:
                raise
            await asyncio.sleep(5)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        text=True,
    )
    return float(out.strip())


EXPORTS = {}


def run_cmd(cmd, cwd, extra=None):
    if cmd.startswith("export ") and "=" in cmd:
        k, v = cmd[7:].split("=", 1)
        EXPORTS[k] = v
        return ""
    env = dict(os.environ, FORCE_COLOR="1", **EXPORTS)
    for k, v in (extra or {}).items():
        env[k] = v.replace("CASSETTE", os.path.join(ROOT, "tests", "fixtures", "cassette.json"))
    env.pop("NO_COLOR", None)
    env["PATH"] = SDKBIN + os.pathsep + env["PATH"]
    env["COLUMNS"] = "120"
    p = subprocess.run(
        ["bash", "-c", "shopt -s nullglob; " + cmd],
        cwd=cwd, capture_output=True, text=True, env=env,
    )
    return (p.stdout + p.stderr).rstrip("\n")


def parse_ansi(line):
    spans, color, bold = [], FG, False
    for part in re.split(r"(\x1b\[[0-9;]*m)", line):
        m = re.match(r"\x1b\[([0-9;]*)m", part)
        if m:
            for code in (m.group(1) or "0").split(";"):
                if code == "0":
                    color, bold = FG, False
                elif code == "1":
                    bold = True
                elif code in ANSI:
                    color = ANSI[code]
        elif part:
            spans.append((part, color, bold))
    return spans


class Term:
    def __init__(self):
        self.font = ImageFont.truetype(MONO, 14)
        self.bold = ImageFont.truetype(MONO_B, 14)
        self.cw = self.font.getlength("M")
        self.lh = 20
        self.cols = int((W - 80) / self.cw)

    def wrap(self, spans):
        lines, cur, n = [], [], 0
        for text, c, b in spans:
            while text:
                room = self.cols - n
                if len(text) <= room:
                    cur.append((text, c, b)); n += len(text); text = ""
                else:
                    cut = text.rfind(" ", 0, room + 1)
                    cut = cut if cut > 0 else room
                    cur.append((text[:cut], c, b)); lines.append(cur); cur, n = [], 0
                    text = "    " + text[cut:].lstrip(" ")
        lines.append(cur)
        return lines

    def frame(self, lines, cursor=False):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([20, 14, W - 20, H - 14], 12, fill=(30, 33, 41), outline=(60, 64, 76))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([40 + i * 24, 30, 54 + i * 24, 44], fill=c)
        d.text((W // 2, 37), "~/lists — bash", font=ImageFont.truetype(SANS, 15), fill=(150, 155, 165), anchor="mm")
        visible = lines[-30:]
        y = 64
        for ln in visible:
            x = 40
            for text, c, b in ln:
                d.text((x, y), text, font=self.bold if b else self.font, fill=c)
                x += self.cw * len(text)
            y += self.lh
        if cursor:
            last = visible[-1] if visible else []
            x = 40 + self.cw * sum(len(t) for t, _, _ in last)
            d.rectangle([x, y - self.lh + 3, x + self.cw - 2, y - 4], fill=FG)
        return img


def card(scene):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    if scene["kind"] == "title":
        d.text((W // 2, 280), scene["title"], font=ImageFont.truetype(MONO_B, 78), fill=(95, 215, 120), anchor="mm")
        d.multiline_text((W // 2, 420), scene["sub"], font=ImageFont.truetype(SANS, 32), fill=FG, anchor="mm",
                         align="center", spacing=12)
        d.text((W // 2, 620), "github.com/hamarshehmhmd/listwarden  ·  audit · fix · pr · GitHub Action  ·  GNU AGPL-3.0",
               font=ImageFont.truetype(SANS, 20), fill=(150, 155, 165), anchor="mm")
    else:
        d.text((90, 90), scene["title"], font=ImageFont.truetype(SANS_B, 52), fill=(95, 215, 120))
        f = ImageFont.truetype(SANS, 29)
        y = 200
        for b in scene["bullets"]:
            if b and not b.startswith("   "):
                d.text((90, y), "•", font=f, fill=(240, 200, 80))
            d.text((130, y), b.strip(), font=f, fill=FG)
            y += 58 if b.startswith("   ") or not b else 62
    return img


class Writer:
    def __init__(self, path, audio):
        self.p = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", audio,
             "-af", "apad", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             "-shortest", path],
            stdin=subprocess.PIPE,
        )
        self.n = 0

    def put(self, img, count=1):
        data = img.tobytes()
        for _ in range(count):
            self.p.stdin.write(data)
        self.n += count

    def close(self):
        self.p.stdin.close()
        self.p.wait()
        if self.p.returncode:
            sys.exit("ffmpeg failed")


def _cap_output(out):
    lines = out.splitlines()
    if len(lines) > 26:
        lines = lines[:24] + ["…", lines[-1]]
    return "\n".join(lines)


def render_scene(i, scene, workdir):
    audio = os.path.join(BUILD, f"s{i}.mp3")
    if not os.path.exists(audio):
        asyncio.run(tts(scene["say"], audio))
    total = int((duration(audio) + 0.7) * FPS)
    path = os.path.join(BUILD, f"s{i}.mp4")
    w = Writer(path, audio)
    if scene["kind"] != "term":
        w.put(card(scene), total)
        w.close()
        return path
    t = Term()
    shown = scene.get("show", scene["cmds"])
    outputs = [_cap_output(run_cmd(c.replace("listwarden ", "listwarden --color always ", 1), workdir, scene.get("env")))
               for c in scene["cmds"]]
    # If show has more lines than cmds (a command displayed as several visual lines
    # that map to one executed command), join show into one typed command per output.
    if len(shown) != len(outputs):
        if len(outputs) == 1:
            shown = [" ".join(s.rstrip("\\").strip() for s in shown)]
        elif len(shown) > len(outputs):
            # first N-1 outputs map 1:1; remaining show lines are the last command
            shown = list(shown[: len(outputs) - 1]) + [
                " ".join(s.rstrip("\\").strip() for s in shown[len(outputs) - 1 :])
            ]
    steps = list(zip(shown, outputs))
    typing = sum(len(s) for s, _ in steps) / 32 * FPS
    pause = max(6, int((total - typing) / max(1, len(steps))) - 4)
    lines = []
    for cmd, out in steps:
        prompt = [(PROMPT, (95, 215, 120), True)]
        for k in range(0, len(cmd) + 1, 2):
            frame = t.frame(lines + t.wrap(prompt + [(cmd[:k], FG, False)]), cursor=True)
            w.put(frame)
        lines += t.wrap(prompt + [(cmd, FG, False)])
        for ol in out.splitlines():
            lines += t.wrap(parse_ansi(ol))
        lines.append([])
        hold = min(pause, max(0, total - w.n))
        w.put(t.frame(lines), hold)
    final = t.frame(lines + t.wrap([(PROMPT, (95, 215, 120), True)]), cursor=True)
    if w.n < total:
        w.put(final, total - w.n)
    w.close()
    return path


def main():
    os.makedirs(BUILD, exist_ok=True)
    if os.path.exists(SANDBOX):
        shutil.rmtree(SANDBOX)
    os.makedirs(SANDBOX)
    repo = SANDBOX
    shutil.copy(os.path.join(ROOT, "tests", "fixtures", "list.md"), os.path.join(SANDBOX, "widgets.md"))
    subprocess.run("gh api repos/qinwf/awesome-R/readme --jq .content | base64 -d > awesome-R.md",
                   shell=True, cwd=SANDBOX, check=True)
    parts = [render_scene(i, s, repo) for i, s in enumerate(SCENES)]
    with open(os.path.join(BUILD, "list.txt"), "w") as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i",
         os.path.join(BUILD, "list.txt"), "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", OUT],
        check=True,
    )
    with open(os.path.join(BUILD, "narration.txt"), "w") as fh:
        fh.write("\n\n".join(s["say"] for s in SCENES) + "\n")
    print(OUT, round(duration(OUT), 1), "s")


if __name__ == "__main__":
    main()
