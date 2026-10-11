# -*- coding: utf-8 -*-
"""
Hánna Podcast 自動更新スクリプト (Spotify Web API 不要版)

1. Spotify の公開埋め込みページ (open.spotify.com/embed/show/{番組ID}) から
   エピソード一覧 (エピソードID・タイトル・公開日) を取得
   - 取得できなければ番組ページ (open.spotify.com/show/{番組ID}) から取得
2. RSS フィードの最新エピソードのタイトルと照合し、最新エピソードIDを決定
   - RSS URL は config で指定、空欄なら Spotify oEmbed で番組名を取得し
     iTunes Search API (無料・認証不要) で自動検索してキャッシュ
   - RSS が使えない場合は Spotify 側の情報だけで最新を判定
3. ローカルの podcast.html の最初の podcast-grid ブロックの直前に挿入
   (同じエピソードIDが既にあればスキップ)
4. ローカルファイルを上書き保存 (バックアップ付き)
5. FTPS でサーバーにアップロード

※ RSS には Spotify のエピソードIDが含まれないため、ID は必ず Spotify の
  公開ページから取得し、RSS は「どれが最新か」の確認に使います。

標準ライブラリのみで動作します (pip install 不要)。

使い方:
    py -3 podcast_updater.py               # 通常実行
    py -3 podcast_updater.py --dry-run     # 何も書き換えず、挿入内容だけログに出す
    py -3 podcast_updater.py --no-upload   # ローカル更新のみ (FTPアップロードしない)
    py -3 podcast_updater.py --force-upload  # 新エピソードが無くてもアップロードする
"""

import argparse
import email.utils
import ftplib
import html as html_lib
import json
import logging
import os
import re
import shutil
import ssl
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "config.json")
STATE_FILE = "state.json"  # 自動検索した RSS URL のキャッシュ

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

EPISODE_BLOCK_TEMPLATE = """<div class="podcast-grid stagger">
  <div class="podcast-card">
    <div class="podcast-card-label">Episode</div>
    <iframe data-testid="embed-iframe" style="border-radius:12px" src="https://open.spotify.com/embed/episode/{episode_id}?utm_source=generator" width="100%" height="152" frameBorder="0" allowfullscreen="" allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture" loading="lazy"></iframe>
  </div>
</div>"""

# 最初の podcast-grid ブロックの開始タグ (行頭インデントも取得する)
FIRST_GRID_RE = re.compile(
    r'^(?P<indent>[ \t]*)<div\s+class="[^"]*\bpodcast-grid\b[^"]*"', re.MULTILINE
)
EPISODE_ID = r"[0-9A-Za-z]{22}"
NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

log = logging.getLogger("podcast_updater")


# ---------------------------------------------------------------- 設定・ログ
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    required = {
        "spotify": ["show_id"],
        "local": ["html_path"],
        "ftp": ["host", "user", "password", "remote_path"],
    }
    for section, keys in required.items():
        for key in keys:
            if not cfg.get(section, {}).get(key):
                raise ValueError(f"config.json に {section}.{key} がありません")
    return cfg


def setup_logging(cfg):
    log_cfg = cfg.get("log", {})
    log_dir = log_cfg.get("dir") or os.path.join(SCRIPT_DIR, "logs")
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(SCRIPT_DIR, log_dir)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, log_cfg.get("file", "podcast_updater.log"))

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)
    log.setLevel(getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO))
    return log_file


def load_state():
    path = os.path.join(SCRIPT_DIR, STATE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    path = os.path.join(SCRIPT_DIR, STATE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- HTTP
def http_get_bytes(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read(), res.headers.get_content_charset()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {url}") from e


def http_get(url, timeout=30):
    body, charset = http_get_bytes(url, timeout)
    return body.decode(charset or "utf-8", errors="replace")


def http_get_json(url):
    return json.loads(http_get(url))


# ---------------------------------------------------------------- 日付・タイトル
def parse_date(value):
    """いろいろな形式の日付を UTC の datetime にする。解釈できなければ None。"""
    if isinstance(value, dict):
        value = value.get("isoString") or value.get("iso") or value.get("date")
    if isinstance(value, (int, float)):
        # ミリ秒 / 秒の UNIX 時刻
        ts = value / 1000 if value > 10**11 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = email.utils.parsedate_to_datetime(s)  # RSS の pubDate 形式
        except (TypeError, ValueError):
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def normalize_title(title):
    t = unicodedata.normalize("NFKC", html_lib.unescape(title or "")).lower()
    return re.sub(r"[\s\W_]+", "", t)


# ---------------------------------------------------------------- Spotify (認証不要)
def _walk_episodes(node, found, order):
    """__NEXT_DATA__ の JSON を再帰的にたどってエピソード情報を集める。"""
    if isinstance(node, dict):
        uri = node.get("uri") or ""
        m = re.fullmatch(r"spotify:episode:(" + EPISODE_ID + ")", uri) if isinstance(uri, str) else None
        if m:
            ep_id = m.group(1)
            ep = found.setdefault(ep_id, {"id": ep_id, "title": "", "date": None, "order": len(order)})
            if ep_id not in order:
                order.append(ep_id)
            title = node.get("title") or node.get("name")
            if isinstance(title, str) and not ep["title"]:
                ep["title"] = title
            for key in ("releaseDate", "release_date", "publishDate", "date"):
                d = parse_date(node.get(key))
                if d and not ep["date"]:
                    ep["date"] = d
        for v in node.values():
            _walk_episodes(v, found, order)
    elif isinstance(node, list):
        for v in node:
            _walk_episodes(v, found, order)


def parse_spotify_page(page):
    """Spotify の埋め込み/番組ページからエピソード一覧 (ページ上の順) を返す。"""
    found, order = {}, []
    m = NEXT_DATA_RE.search(page)
    if m:
        try:
            _walk_episodes(json.loads(m.group(1)), found, order)
        except ValueError:
            log.warning("__NEXT_DATA__ の JSON を解析できませんでした")
    if not found:
        # JSON 構造が変わった場合の保険: ページ内のエピソードIDを出現順に拾う
        pattern = r"(?:spotify:episode:|/episode/)(" + EPISODE_ID + ")"
        for ep_id in re.findall(pattern, page):
            if ep_id not in found:
                found[ep_id] = {"id": ep_id, "title": "", "date": None, "order": len(order)}
                order.append(ep_id)
    return [found[i] for i in order]


def fetch_spotify_episodes(show_id):
    urls = [
        f"https://open.spotify.com/embed/show/{show_id}",
        f"https://open.spotify.com/show/{show_id}",
    ]
    last_error = None
    for url in urls:
        try:
            episodes = parse_spotify_page(http_get(url))
            if episodes:
                log.info("Spotify ページから %d 件のエピソードを取得: %s", len(episodes), url)
                return episodes
            log.warning("エピソードが見つかりませんでした: %s", url)
        except Exception as e:
            last_error = e
            log.warning("取得失敗: %s (%s)", url, e)
    raise RuntimeError(f"Spotify からエピソードを取得できませんでした: {last_error}")


def get_show_title(show_id):
    url = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(
        f"https://open.spotify.com/show/{show_id}", safe=""
    )
    return http_get_json(url).get("title", "")


# ---------------------------------------------------------------- RSS
def discover_rss_url(show_id, country):
    """oEmbed で番組名を取得し、iTunes Search API で RSS URL を探す。"""
    title = get_show_title(show_id)
    if not title:
        raise RuntimeError("oEmbed から番組名を取得できませんでした")
    log.info("番組名 (oEmbed): %s", title)
    params = urllib.parse.urlencode({
        "term": title, "media": "podcast", "entity": "podcast",
        "country": country, "limit": 20,
    })
    results = http_get_json(f"https://itunes.apple.com/search?{params}").get("results", [])
    candidates = [r for r in results if r.get("feedUrl")]
    target = normalize_title(title)
    for r in candidates:
        if normalize_title(r.get("collectionName")) == target:
            return r["feedUrl"]
    raise RuntimeError(
        f"iTunes で番組「{title}」の RSS が見つかりませんでした "
        f"(候補: {[r.get('collectionName') for r in candidates[:5]]})"
    )


def get_rss_url(cfg, state):
    rss_cfg = cfg.get("rss", {})
    if rss_cfg.get("url"):
        return rss_cfg["url"]
    show_id = cfg["spotify"]["show_id"]
    cached = state.get("rss_url", {})
    if cached.get("show_id") == show_id and cached.get("url"):
        return cached["url"]
    url = discover_rss_url(show_id, rss_cfg.get("country", "JP"))
    log.info("RSS URL を自動検出しました: %s", url)
    state["rss_url"] = {"show_id": show_id, "url": url}
    save_state(state)
    return url


def fetch_rss_latest(rss_url):
    # XML は宣言された文字コードで解析させるためバイト列のまま渡す
    body, _ = http_get_bytes(rss_url)
    root = ET.fromstring(body)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        date = parse_date(item.findtext("pubDate"))
        if title:
            items.append({"title": title, "date": date})
    if not items:
        raise RuntimeError("RSS にエピソードがありません")
    dated = [i for i in items if i["date"]]
    return max(dated, key=lambda i: i["date"]) if dated else items[0]


# ---------------------------------------------------------------- 最新エピソード決定
def pick_latest_episode(cfg):
    episodes = fetch_spotify_episodes(cfg["spotify"]["show_id"])

    # RSS の最新タイトルと照合できればそれを採用
    if cfg.get("rss", {}).get("enabled", True):
        try:
            state = load_state()
            latest = fetch_rss_latest(get_rss_url(cfg, state))
            log.info("RSS 最新: %s (%s)", latest["title"], latest["date"])
            key = normalize_title(latest["title"])
            for ep in episodes:
                if ep["title"] and normalize_title(ep["title"]) == key:
                    log.info("RSS とタイトルが一致したエピソードを採用します")
                    return ep
            log.warning("RSS の最新タイトルと一致する Spotify エピソードがありません。"
                        "Spotify 側の情報で判定します")
        except Exception as e:
            log.warning("RSS を利用できませんでした (Spotify 側の情報で判定します): %s", e)

    # Spotify 側: 公開日があれば最新日付、無ければページ上の先頭 (新しい順に並んでいる)
    dated = [e for e in episodes if e["date"]]
    if dated:
        return max(dated, key=lambda e: (e["date"], -e["order"]))
    return episodes[0]


# ---------------------------------------------------------------- HTML
def read_text(path):
    # newline="" で改行コード (CRLF/LF) をそのまま保持する
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return f.read()


def build_block(episode_id, indent, newline):
    lines = EPISODE_BLOCK_TEMPLATE.format(episode_id=episode_id).split("\n")
    return newline.join(indent + line for line in lines) + newline


def insert_episode(html, episode_id):
    """新しい HTML を返す。既に存在する場合は None を返す。"""
    if re.search(r"/embed/episode/" + re.escape(episode_id) + r"\b", html):
        return None

    match = FIRST_GRID_RE.search(html)
    if not match:
        raise RuntimeError('podcast.html に <div class="podcast-grid ..."> が見つかりません')

    newline = "\r\n" if "\r\n" in html else "\n"
    indent = match.group("indent")
    block = build_block(episode_id, indent, newline)
    pos = match.start()  # 行頭 (インデント含む) の位置に挿入
    return html[:pos] + block + html[pos:]


def write_with_backup(path, content, backup_dir, keep):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name, ext = os.path.splitext(os.path.basename(path))
    backup_path = os.path.join(backup_dir, f"{name}_{stamp}{ext}")
    shutil.copy2(path, backup_path)
    log.info("バックアップ作成: %s", backup_path)

    # 一時ファイルに書いてから置き換え (書き込み途中で壊れるのを防ぐ)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.replace(tmp_path, path)

    # 古いバックアップを削除
    backups = sorted(
        f for f in os.listdir(backup_dir) if f.startswith(name + "_") and f.endswith(ext)
    )
    for old in backups[:-keep] if keep > 0 else []:
        os.remove(os.path.join(backup_dir, old))


# ---------------------------------------------------------------- FTPS
def upload_ftps(ftp_cfg, local_path):
    host = ftp_cfg["host"]
    port = int(ftp_cfg.get("port", 21))
    remote_path = ftp_cfg["remote_path"]
    remote_dir, remote_name = remote_path.rsplit("/", 1)
    tmp_name = remote_name + ".uploading"

    context = ssl.create_default_context()
    with ftplib.FTP_TLS(context=context, timeout=60, encoding="utf-8") as ftp:
        ftp.connect(host, port)
        ftp.login(ftp_cfg["user"], ftp_cfg["password"])
        ftp.prot_p()  # データ通信も暗号化
        ftp.set_pasv(bool(ftp_cfg.get("passive", True)))
        if remote_dir:
            ftp.cwd(remote_dir)
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {tmp_name}", f)
        # 一時名でアップロード後にリネーム (公開中ファイルが途中状態にならないように)
        try:
            ftp.delete(remote_name)
        except ftplib.error_perm:
            pass  # 初回などでファイルが無い場合
        ftp.rename(tmp_name, remote_name)
        size = ftp.size(remote_name)
    log.info("FTPS アップロード完了: %s:%s (%s bytes)", host, remote_path, size)


# ---------------------------------------------------------------- メイン
def main():
    parser = argparse.ArgumentParser(description="Hánna Podcast 自動更新")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="config.json のパス")
    parser.add_argument("--dry-run", action="store_true", help="ファイル更新・アップロードをしない")
    parser.add_argument("--no-upload", action="store_true", help="FTP アップロードをしない")
    parser.add_argument("--force-upload", action="store_true", help="変更が無くてもアップロードする")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"設定ファイルの読み込みに失敗しました: {e}", file=sys.stderr)
        return 2

    log_file = setup_logging(cfg)
    log.info("===== 実行開始 (ログ: %s) =====", log_file)

    try:
        html_path = cfg["local"]["html_path"]
        backup_dir = cfg["local"].get("backup_dir") or os.path.join(SCRIPT_DIR, "backup")
        if not os.path.isabs(backup_dir):
            backup_dir = os.path.join(SCRIPT_DIR, backup_dir)
        keep = int(cfg["local"].get("backup_keep", 10))

        episode = pick_latest_episode(cfg)
        episode_id = episode["id"]
        log.info("最新エピソード: %s (%s) id=%s",
                 episode.get("title") or "(タイトル不明)", episode.get("date"), episode_id)

        if not os.path.isfile(html_path):
            raise FileNotFoundError(f"ローカルファイルが見つかりません: {html_path}")
        html = read_text(html_path)
        new_html = insert_episode(html, episode_id)

        if new_html is None:
            log.info("エピソード %s は既に掲載済みのためスキップします", episode_id)
            changed = False
        elif args.dry_run:
            log.info("[dry-run] 以下のブロックを挿入予定:\n%s", build_block(episode_id, "", "\n"))
            changed = False
        else:
            write_with_backup(html_path, new_html, backup_dir, keep)
            log.info("ローカルファイルを更新しました: %s", html_path)
            changed = True

        if args.dry_run or args.no_upload:
            log.info("アップロードは行いません (--dry-run / --no-upload)")
        elif changed or args.force_upload:
            upload_ftps(cfg["ftp"], html_path)
        else:
            log.info("変更が無いためアップロードしません")

        log.info("===== 正常終了 =====")
        return 0
    except Exception:
        log.exception("エラーが発生しました")
        log.info("===== 異常終了 =====")
        return 1


if __name__ == "__main__":
    sys.exit(main())
