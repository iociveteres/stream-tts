"""Скачивает реплики персонажа со страницы MediaWiki (fandom, theportalwiki и т. п.).

Использование:
    python tools/sources/fetch-wiki-lines.py <api.php> <страница> <папка> [--filter РЕГЭКСП]

Берёт каждый <li>, где есть аудиофайл, и сохраняет его как <папка>/NNN_<имя>.<ext>.
Текст реплики (то, что видно в <li> без кнопки плеера) пишется в <папка>/lines.json.
Ссылки берутся с той же страницы вики, ничего не исполняется.
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (local-voice-gen; personal use)"}


def get(url, referer=None):
    headers = dict(UA, Referer=referer) if referer else UA
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def clean_text(li):
    li = re.sub(r"<audio.*?</audio>", "", li, flags=re.S)
    li = re.sub(r"<sup.*?</sup>", "", li, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", "", li))
    t = t.replace("▶️", "").replace("▶", "").replace("️", "")
    t = re.sub(r"^\s*(н\s+)?", "", t)          # пометка «н» на вики Dota 2
    return re.sub(r"\s+", " ", t).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("api")
    ap.add_argument("page")
    ap.add_argument("out")
    ap.add_argument("--filter", help="брать только файлы, чьё имя подходит под регэксп")
    a = ap.parse_args()

    q = urllib.parse.urlencode({"action": "parse", "page": a.page, "prop": "text", "format": "json"})
    page = json.loads(get(f"{a.api}?{q}"))["parse"]["text"]["*"]

    os.makedirs(a.out, exist_ok=True)
    lines, seen = [], set()
    for li in re.findall(r"<li>(.*?)</li>", page, re.S):
        srcs = re.findall(r'<source src="([^"]+)"', li) or re.findall(r'<audio[^>]+src="([^"]+)"', li)
        if not srcs:
            continue
        src = html.unescape(srcs[0])
        if src.startswith("//"):
            src = "https:" + src
        name = urllib.parse.unquote(re.search(r"/([^/]+\.(?:mp3|ogg|wav|flac))", src, re.I).group(1))
        if name in seen or (a.filter and not re.search(a.filter, name, re.I)):
            continue
        seen.add(name)
        fn = f"{len(lines):03d}_{name}"
        path = os.path.join(a.out, fn)
        if not os.path.exists(path):
            try:
                # CDN fandom отдаёт файлы только с Referer самой вики
                data = get(src, referer=a.api.rsplit("/", 1)[0] + "/")
            except Exception as e:  # noqa: BLE001
                print(f"!! {name}: {e}", file=sys.stderr)
                continue
            with open(path, "wb") as f:
                f.write(data)
        lines.append({"file": fn, "text": clean_text(li), "src": src})

    with open(os.path.join(a.out, "lines.json"), "w", encoding="utf-8") as f:
        json.dump(lines, f, ensure_ascii=False, indent=1)
    print(f"{len(lines)} реплик -> {a.out}")


if __name__ == "__main__":
    main()
