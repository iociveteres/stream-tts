"""Docker для пользователя — готовый образ из GHCR и один docker-compose.yml (README, «Установка» → «Docker»): без
процесса stream-tts и без docker, секунды.

- docker-compose.yml берёт образ ghcr.io/<репозиторий>:latest — тот, что публикует docker.yml по тегу, и того же
  репозитория, откуда README велит скачать compose-файлы; сборки (build) в нём нет — у пользователя нет Dockerfile;
- docker-compose.yml монтирует только папки данных: папка проекта, которой нет рядом со скачанным файлом, создалась бы
  пустой и закрыла бы собой файлы образа (эффекты в /project/effects);
- docker-compose.dev.yml собирает образ под своим именем без реестра: собранный не подменяет скачанный;
- эффекты (effects/*.py) попадают в образ в STREAM_TTS_PROJECT/effects: иначе post наборов голосов не работает;
- тексты лицензий образа (packaging/linux/licenses.txt) — те же файлы, адреса и sha256, что у программы для Windows
  (LICENSE_URLS в packaging/windows/build-runtime.py)."""
import importlib.util, os, re

from harness import STREAM_TTS

ROOT = os.path.dirname(STREAM_TTS)
# Папки данных пользователя: создаются рядом с docker-compose.yml и в git не входят
DATA = {"stream-tts-data", "models", "espeech-runtime", "cuda", "voices"}


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def code(text):
    """Строки без комментариев #."""
    return [line for line in (re.sub(r"(^|\s)#.*", "", line).rstrip() for line in text.splitlines()) if line.strip()]


def test_public_compose_uses_published_image():
    lines = code(read("docker-compose.yml"))
    images = [m.group(1) for line in lines if (m := re.match(r"\s+image:\s*(\S+)$", line))]
    assert len(images) == 1, images
    m = re.fullmatch(r"ghcr\.io/([a-z0-9._-]+/[a-z0-9._-]+):latest", images[0])
    assert m, f"docker-compose.yml: образ не из GHCR с тегом latest: {images[0]}"
    repo = m.group(1)
    assert not any(re.match(r"\s+build:", line) for line in lines), "docker-compose.yml: build — у пользователя нет Dockerfile"

    # README велит скачать compose-файлы из того же репозитория, и они есть в репозитории
    readme = read("README.md")
    urls = re.findall(r"https://raw\.githubusercontent\.com/([\w.-]+/[\w.-]+)/main/([\w.-]+)", readme)
    assert {f for _, f in urls} >= {"docker-compose.yml", "docker-compose.gpu.yml"}, urls
    for r, f in urls:
        assert r.lower() == repo, f"README качает {f} из {r}, а образ — ghcr.io/{repo}"
        assert os.path.isfile(os.path.join(ROOT, f)), f

    # docker.yml публикует ghcr.io/<репозиторий>:<версия> и :latest
    wf = read(".github/workflows/docker.yml")
    assert "ghcr.io/${GITHUB_REPOSITORY,,}" in wf
    assert re.search(r'for tag in "\$VERSION" latest; do', wf), "docker.yml не публикует :latest"


def test_public_compose_mounts_only_data():
    binds = re.findall(r"^\s+-\s*\./([^:/]+)[^:]*:", "\n".join(code(read("docker-compose.yml"))), re.M)
    assert binds, "docker-compose.yml: нет папок данных"
    extra = set(binds) - DATA
    assert not extra, f"docker-compose.yml монтирует папки проекта {extra}: у пользователя без репозитория их нет"


def test_dev_compose_builds_under_own_name():
    lines = code(read("docker-compose.dev.yml"))
    assert any(re.match(r"\s+build:", line) for line in lines)
    images = [m.group(1) for line in lines if (m := re.match(r"\s+image:\s*(\S+)$", line))]
    assert images and "/" not in images[0], f"docker-compose.dev.yml: собранный образ должен называться без реестра: {images}"


def test_effects_in_image():
    df = read("Dockerfile")
    project = re.search(r"^\s*STREAM_TTS_PROJECT=(\S+)", df, re.M).group(1)
    assert re.search(rf"^COPY effects/\*\.py {re.escape(project)}/effects/$", df, re.M), \
        f"Dockerfile: эффекты не копируются в {project}/effects"
    assert "!effects/*.py" in read(".dockerignore").splitlines()


def test_image_licenses_match_windows():
    spec = importlib.util.spec_from_file_location("build_runtime", os.path.join(ROOT, "packaging", "windows", "build-runtime.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    linux = {}
    for line in read("packaging/linux/licenses.txt").splitlines():
        if line.strip() and not line.startswith("#"):
            name, sha, url = line.split()
            linux[name] = (url, sha)
    assert linux == mod.LICENSE_URLS
