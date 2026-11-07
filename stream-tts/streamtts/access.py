"""Доступ к админке: пароль и откуда пускать без него.

Пароль — переменная STREAM_TTS_PASSWORD (главнее, админка её не меняет) или файл <данные>/admin-password со
scrypt-хэшем: его задают, меняют и снимают в админке («Настройки» → «Сервер»). Забыли пароль — удалить файл и
перезапустить. Без пароля пускаются только адреса локальной сети по IP или локальному имени (local_request)."""
import base64, hashlib, hmac, ipaddress, os, secrets, time

from . import config

PATH = os.path.join(config.DATA, "admin-password")
MIN_LEN = 8          # с паролем админка открыта в интернет, а вход — одна попытка в секунду
# scrypt: n=2**14, r=8 — 16 МБ памяти и ~50 мс на проверку (рекомендация OWASP — не меньше)
N, R, P = 1 << 14, 8, 1
# Имена без пароля: локальные зоны домашних сетей. Публичное имя (DDNS, туннель) или чужой сайт с DNS rebinding
# на адрес в сети приходят с точкой в имени и обычным доменом
LOCAL_ZONES = (".localhost", ".local", ".lan", ".home.arpa", ".internal")
# Заголовки прокси и туннелей (cloudflared, nginx): за ними настоящий адрес клиента неизвестен
PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP")

_stored = None       # строка хэша из файла, "" — файла нет; None — ещё не читали
refused = None       # последний отказ без пароля «не из локальной сети»: dict(ts, addr, host) для админки


def _b64(b):
    return base64.b64encode(b).decode()


def _scrypt(password, salt, n=N, r=R, p=P):
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 << 20, dklen=32)


def stored():
    global _stored
    if _stored is None:
        try:
            with open(PATH, encoding="utf-8") as f:
                _stored = f.read().strip()
        except OSError:
            _stored = ""
    return _stored


def source():
    """→ "env" (переменная окружения), "file" (задан в админке) или None (пароля нет)."""
    return "env" if config.PASSWORD else "file" if stored() else None


def fingerprint():
    """Основа токена cookie: меняется с паролем (у файла — новая соль, даже если пароль тот же), и старые входы
    перестают работать. У переменной — сам пароль, как до появления файла: входы переживают обновление."""
    return config.PASSWORD or stored()


def check(password):
    """Верный ли пароль. Хэш файла — ~50 мс процессора: вызывать из потока (asyncio.to_thread)."""
    # байты: compare_digest со строками не-ASCII (пароль по-русски) бросает TypeError
    if config.PASSWORD:
        return hmac.compare_digest(password.encode(), config.PASSWORD.encode())
    line = stored()
    if not line:
        return False
    try:
        _, n, r, p, salt, digest = line.split("$")
        got = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except ValueError:   # файл испорчен вручную: войти нельзя, восстановление — удалить его
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


def set_password(password):
    """Записать хэш нового пароля (пустой — снять пароль). Хэш — ~50 мс: из потока."""
    global _stored
    if not password:
        try:
            os.remove(PATH)
        except FileNotFoundError:
            pass
        _stored = ""
        return
    salt = secrets.token_bytes(16)
    line = f"scrypt${N}${R}${P}${_b64(salt)}${_b64(_scrypt(password, salt))}"
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(line + "\n")
    os.replace(tmp, PATH)   # обрыв посреди записи не оставит пустой файл, с которым не войти
    _stored = line


def local_address(addr):
    """Адрес клиента — из локальной сети: частный, loopback, link-local, а также 100.64.0.0/10 (Tailscale, CGNAT) —
    всё, что не маршрутизируется в интернете."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except (ValueError, AttributeError):
        return False
    if ip.version == 6 and ip.ipv4_mapped:   # ::ffff:192.168.1.5 — IPv4 через сокет IPv6
        ip = ip.ipv4_mapped
    return not ip.is_global and not ip.is_multicast and not ip.is_unspecified


def local_host(host):
    """Host — адрес в локальной сети или локальное имя: IP не из интернета, localhost, имя без точки (homeserver),
    .local, .lan, .home.arpa, .internal. Иначе это DNS rebinding (чужой сайт, чьё имя ведёт на адрес в сети),
    публичное имя (DDNS, туннель) или публичный IP при пробросе порта на роутере."""
    name = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
    name = name.lower().rstrip(".")
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return bool(name) and (name == "localhost" or "." not in name or name.endswith(LOCAL_ZONES))
    return local_address(name)


def local_request(remote, host, headers):
    """Запрос из локальной сети — его можно пустить без пароля. В Docker Desktop адрес клиента — всегда частный адрес
    шлюза, поэтому проверяется и имя в Host: из интернета приходят по публичному имени или IP."""
    if any(h in headers for h in PROXY_HEADERS):
        return False
    return local_address(remote or "") and local_host(host or "")


def client_addr(remote, headers):
    """Адрес клиента для подсказки: за туннелем или прокси remote — их адрес (127.0.0.1), настоящий — в заголовке."""
    for h in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
        if v := headers.get(h, "").split(",")[0].strip():
            return v
    return remote or ""


def refuse(remote, host, headers):
    """Запомнить отказ без пароля — админка подскажет, что пароль нужен. → True, если адрес новый (для лога)."""
    global refused
    addr = client_addr(remote, headers)
    new = not refused or refused["addr"] != addr or refused["host"] != host
    refused = dict(ts=time.time(), addr=addr, host=host or "")
    return new
