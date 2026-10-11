# -*- coding: utf-8 -*-
"""
Hánna Podcast 自動更新スクリプト

1. Spotify API (Client Credentials) でアクセストークンを取得
2. 番組の最新エピソードを取得
3. ローカルの podcast.html の最初の podcast-grid ブロックの直前に挿入
   (同じエピソードIDが既にあればスキップ)
4. ローカルファイルを上書き保存 (バックアップ付き)
5. FTPS でサーバーにアップロード

標準ライブラリのみで動作します (pip install 不要)。

使い方:
    py -3 podcast_updater.py               # 通常実行
    py -3 podcast_updater.py --dry-run     # 何も書き換えず、挿入内容だけログに出す
    py -3 podcast_updater.py --no-upload   # ローカル更新のみ (FTPアップロードしない)
    py -3 podcast_updater.py --force-upload  # 新エピソードが無くてもアップロードする
"""

import argparse
import base64
import ftplib
import json
import logging
import os
import re
import shutil
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from logging.handlers import RotatingFileHandler

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "config.json")

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

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

log = logging.getLogger("podcast_updater")


# ---------------------------------------------------------------- 設定・ログ
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    required = {
        "spotify": ["client_id", "client_secret", "show_id"],
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


# ---------------------------------------------------------------- Spotify
def http_request(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {body}") from e


def get_access_token(client_id, client_secret):
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    res = http_request(
        SPOTIFY_TOKEN_URL,
        data=data,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    token = res.get("access_token")
    if not token:
        raise RuntimeError(f"アクセストークンを取得できませんでした: {res}")
    return token


def get_latest_episode(token, show_id, market):
    # Client Credentials では market 指定が必須 (未指定だと空になることがある)。
    # 並び順に依存しないよう数件取得し、公開日が最新のものを選ぶ。
    params = urllib.parse.urlencode({"market": market, "limit": 10})
    url = f"{SPOTIFY_API_BASE}/shows/{show_id}/episodes?{params}"
    res = http_request(url, headers={"Authorization": f"Bearer {token}"})
    episodes = [e for e in res.get("items", []) if e and e.get("id")]
    if not episodes:
        raise RuntimeError("エピソードが1件も取得できませんでした (番組ID / market を確認してください)")
    return max(episodes, key=lambda e: e.get("release_date") or "")


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
        sp = cfg["spotify"]
        html_path = cfg["local"]["html_path"]
        backup_dir = cfg["local"].get("backup_dir") or os.path.join(SCRIPT_DIR, "backup")
        keep = int(cfg["local"].get("backup_keep", 10))

        token = get_access_token(sp["client_id"], sp["client_secret"])
        log.info("Spotify アクセストークン取得 OK")

        episode = get_latest_episode(token, sp["show_id"], sp.get("market", "JP"))
        episode_id = episode["id"]
        log.info("最新エピソード: %s (%s) id=%s",
                 episode.get("name"), episode.get("release_date"), episode_id)

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
