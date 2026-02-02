#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import random
import re
import shutil
import sys
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter


# -------------------------
# Defaults
# -------------------------
DEFAULT_MAX_WIDTH = 2000     # Qualité identique
DEFAULT_WORKERS = 4          # 3–8 en général ok
DEFAULT_SLEEP = 0.0          # 0 pour vitesse
TIMEOUT = 60
MAX_TRIES = 8

BASE_BACKOFF = 0.6
MAX_BACKOFF = 12.0

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0 Safari/537.36"
)

_tls = threading.local()


# -------------------------
# URL / ARK parsing
# -------------------------
ARK_RE = re.compile(r"/ark:/12148/([^/?#]+)", re.IGNORECASE)

def ark_from_url(url: str) -> str:
    """
    Extrait l'ARK depuis une URL Gallica, même si elle contient des query params.
    Ex: https://gallica.bnf.fr/ark:/12148/bd6t54208770t?rk=107296;4
        -> bd6t54208770t
    """
    m = ARK_RE.search(url)
    if not m:
        raise ValueError("Impossible d'extraire l'ARK depuis l'URL (attendu: .../ark:/12148/<ARK>...).")
    return m.group(1).strip()


# -------------------------
# HTTP helpers
# -------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Referer": "https://gallica.bnf.fr/",
        "Origin": "https://gallica.bnf.fr",
    })
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch(session: requests.Session, url: str, expect_json: bool = False, stream: bool = False) -> requests.Response:
    r = session.get(url, timeout=TIMEOUT, stream=stream)
    r.raise_for_status()
    if expect_json:
        _ = r.json()
    return r


def warmup(session: requests.Session, ark: str) -> None:
    # Ouvre une page Gallica "viewer" pour récupérer cookies / init session
    urls = [
        f"https://gallica.bnf.fr/ark:/12148/{ark}/f1.item",
        f"https://gallica.bnf.fr/ark:/12148/{ark}",
    ]
    last_err = None
    for u in urls:
        try:
            session.get(u, timeout=TIMEOUT)
            return
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err


# -------------------------
# IIIF manifest
# -------------------------
def manifest_url(ark: str) -> str:
    return f"https://gallica.bnf.fr/iiif/ark:/12148/{ark}/manifest.json"


def extract_manifest_from_html(html: str) -> str | None:
    m = re.search(r"https://gallica\.bnf\.fr/iiif/ark:/12148/[^\"']+/manifest\.json", html)
    if m:
        return m.group(0)
    m = re.search(r"/iiif/ark:/12148/[^\"']+/manifest\.json", html)
    if m:
        return "https://gallica.bnf.fr" + m.group(0)
    return None


def get_manifest(session: requests.Session, ark: str) -> dict:
    murl = manifest_url(ark)

    # 1) Essai direct
    try:
        r = fetch(session, murl, expect_json=True)
        return r.json()
    except requests.HTTPError as e:
        if e.response is None:
            raise
        if e.response.status_code != 403:
            raise

    # 2) Si 403, retente après warmup
    for attempt in range(1, 4):
        time.sleep(0.7 * attempt)
        try:
            warmup(session, ark)
            r = fetch(session, murl, expect_json=True)
            return r.json()
        except Exception:
            pass

    # 3) Fallback HTML: parse manifest depuis viewer
    html_urls = [
        f"https://gallica.bnf.fr/ark:/12148/{ark}/f1.item",
        f"https://gallica.bnf.fr/ark:/12148/{ark}",
    ]
    for u in html_urls:
        try:
            rr = session.get(u, timeout=TIMEOUT)
            rr.raise_for_status()
            alt = extract_manifest_from_html(rr.text)
            if alt:
                r = fetch(session, alt, expect_json=True)
                return r.json()
        except Exception:
            continue

    raise RuntimeError(
        "403 sur le manifest IIIF et aucun manifest alternatif trouvé dans le HTML.\n"
        "→ Essaie depuis un autre réseau (4G), ou ouvre la page Gallica une fois dans ton navigateur, puis relance."
    )


def iter_canvases(manifest: dict):
    # IIIF v2: manifest['sequences'][0]['canvases']
    if "sequences" in manifest and manifest["sequences"]:
        seq0 = manifest["sequences"][0]
        yield from seq0.get("canvases", [])
        return

    # IIIF v3: manifest['items']
    if "items" in manifest:
        yield from manifest.get("items", [])
        return


def canvas_image_service_id(canvas: dict) -> str:
    # v2
    imgs = canvas.get("images")
    if imgs:
        res = imgs[0].get("resource", {})
        svc = res.get("service", {})
        sid = svc.get("@id") or svc.get("id")
        if sid:
            return sid

    # v3 (rare)
    items = canvas.get("items", [])
    if items:
        try:
            annopage = items[0]
            annos = annopage["items"]
            body = annos[0]["body"]
            svc = body.get("service")
            if isinstance(svc, list) and svc:
                return svc[0].get("id") or svc[0].get("@id")
        except Exception:
            pass

    raise RuntimeError("Impossible de trouver le service IIIF image pour une page (structure inattendue).")


def iiif_jpg_url(service_id: str, max_width: int) -> str:
    service_id = service_id.rstrip("/")
    return f"{service_id}/full/{max_width},/0/default.jpg"


# -------------------------
# Download with backoff
# -------------------------
def thread_session(cookies: dict) -> requests.Session:
    if not hasattr(_tls, "session"):
        _tls.session = make_session()
        if cookies:
            _tls.session.cookies.update(cookies)
    return _tls.session


def download_with_backoff(url: str, out_path: Path, cookies: dict, sleep: float) -> None:
    # skip si déjà ok
    if out_path.exists() and out_path.stat().st_size > 50_000:
        return

    s = thread_session(cookies)

    base = BASE_BACKOFF
    for attempt in range(1, MAX_TRIES + 1):
        r = s.get(url, stream=True, timeout=TIMEOUT)

        if r.status_code == 200:
            tmp = out_path.with_suffix(out_path.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp, out_path)
            if sleep and sleep > 0:
                time.sleep(sleep)
            return

        if r.status_code in (403, 429, 503):
            wait = base * (2 ** (attempt - 1)) + random.uniform(0, 0.35)
            time.sleep(min(wait, MAX_BACKOFF))
            continue

        r.raise_for_status()

    raise RuntimeError(f"Echec téléchargement après {MAX_TRIES} essais: {url}")


def parallel_download(jobs, workers: int, cookies: dict, sleep: float) -> None:
    total = len(jobs)
    if total == 0:
        return

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [
            ex.submit(download_with_backoff, url, out_path, cookies, sleep)
            for (url, out_path) in jobs
        ]
        for _ in as_completed(futs):
            done += 1
            if done % 25 == 0 or done == total:
                print(f"Téléchargées: {done}/{total}")


# -------------------------
# PDF assembly
# -------------------------
def assemble_pdf(images: list[Path], out_pdf: Path) -> None:
    # Essai img2pdf (recommandé)
    try:
        import img2pdf
        with open(out_pdf, "wb") as f:
            f.write(img2pdf.convert([str(p) for p in images]))
        return
    except Exception:
        pass

    # Fallback Pillow
    try:
        from PIL import Image
        pil_images = []
        for p in images:
            im = Image.open(p).convert("RGB")
            pil_images.append(im)
        first, rest = pil_images[0], pil_images[1:]
        first.save(out_pdf, save_all=True, append_images=rest)
        return
    except Exception as e:
        raise RuntimeError(
            "Impossible d'assembler en PDF. Installe img2pdf ou pillow:\n"
            "  python3 -m pip install img2pdf\n"
            "ou\n"
            "  python3 -m pip install pillow\n"
            f"Erreur: {e}"
        )


# -------------------------
# Safe cleanup
# -------------------------
def safe_rmtree(workdir: Path, ark: str) -> bool:
    """
    Supprime le dossier parent de travail "définitivement" (rmtree),
    avec garde-fous pour éviter une connerie (genre rm -rf ~/).

    Retourne True si supprimé, False si refusé.
    """
    workdir = workdir.resolve()

    # Garde-fous basiques
    forbidden = {
        Path("/").resolve(),
        Path.home().resolve(),
        Path.cwd().resolve(),
    }
    if workdir in forbidden:
        print(f"⚠️ Refus suppression (dossier critique): {workdir}")
        return False

    # On accepte uniquement un dossier qui ressemble à gallica_<ark> (par défaut)
    expected = f"gallica_{ark}"
    if workdir.name != expected:
        print(f"⚠️ Refus suppression (nom inattendu): {workdir.name} (attendu: {expected})")
        print("    → Si tu veux un dossier custom, garde-le avec --keep ou renomme-le au format gallica_<ark>.")
        return False

    # Supprimer
    shutil.rmtree(workdir, ignore_errors=False)
    return True


# -------------------------
# Main
# -------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Télécharger un ouvrage Gallica via IIIF et l'assembler en PDF.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    ap.add_argument(
        "--url", default=None,
        help="URL Gallica contenant l'ARK (ex: https://gallica.bnf.fr/ark:/12148/<ARK>/f1.item)"
    )
    ap.add_argument(
        "--ark", default=None,
        help="ARK seul (ex: bd6t542069393). Utilisé si --url n'est pas fourni."
    )

    ap.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH,
                    help="Largeur max IIIF (qualité). Garde la même valeur pour garder la même qualité.")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="Téléchargements parallèles.")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                    help="Pause (secondes) après chaque page par worker (0 pour vitesse).")

    ap.add_argument("--out", default=None,
                    help="Nom du PDF final (défaut: <ark>.pdf).")
    ap.add_argument("--dir", default=None,
                    help="Dossier de travail (défaut: ./gallica_<ark>/).")

    # Important: par défaut on SUPPRIME, et --keep désactive
    ap.add_argument("--keep", action="store_true",
                    help="Conserver le dossier de travail (désactive la suppression automatique).")

    args = ap.parse_args()

    if not args.url and not args.ark:
        ap.error("Il faut fournir --url ou --ark.")

    if args.url:
        ark = ark_from_url(args.url)
    else:
        ark = args.ark.strip()

    out_pdf = Path(args.out) if args.out else Path(f"{ark}.pdf")
    workdir = Path(args.dir) if args.dir else Path(f"gallica_{ark}")
    img_dir = workdir / "images"

    img_dir.mkdir(parents=True, exist_ok=True)

    master = make_session()

    print("0) Warmup (page Gallica pour cookies)…")
    try:
        warmup(master, ark)
    except Exception as e:
        print(f"⚠️ Warmup non bloquant (erreur: {e})")

    print("1) Téléchargement manifest…")
    manifest = get_manifest(master, ark)

    canvases = list(iter_canvases(manifest))
    if not canvases:
        raise RuntimeError("Aucune page trouvée dans le manifest (structure inattendue).")

    print(f"2) {len(canvases)} pages détectées.")

    cookies = master.cookies.get_dict()

    # Build jobs
    jobs = []
    for i, canvas in enumerate(canvases, start=1):
        out_img = img_dir / f"page_{i:04d}.jpg"
        if out_img.exists() and out_img.stat().st_size > 50_000:
            continue
        sid = canvas_image_service_id(canvas)
        url = iiif_jpg_url(sid, max_width=args.max_width)
        jobs.append((url, out_img))

    print(f"3) Pages à télécharger: {len(jobs)} (déjà présentes: {len(canvases) - len(jobs)})")
    if jobs:
        print(f"   → workers={args.workers} sleep={args.sleep} max_width={args.max_width}")
        parallel_download(jobs, workers=args.workers, cookies=cookies, sleep=args.sleep)

    # Assemble PDF
    print("4) Assemblage PDF…")
    images = [img_dir / f"page_{i:04d}.jpg" for i in range(1, len(canvases) + 1)]
    missing = [p for p in images if not p.exists()]
    if missing:
        raise RuntimeError(f"Il manque {len(missing)} images (ex: {missing[0]}). Relance le script.")

    assemble_pdf(images, out_pdf)
    print(f"✅ PDF généré: {out_pdf.resolve()}")

    # Cleanup by default
    if not args.keep:
        print("5) Suppression automatique du dossier de travail…")
        try:
            deleted = safe_rmtree(workdir, ark)
            if deleted:
                print(f"🧹 Dossier supprimé: {workdir.resolve()}")
            else:
                print("🧷 Dossier conservé (garde-fous).")
        except Exception as e:
            print(f"⚠️ Échec suppression (dossier conservé): {e}")
    else:
        print("5) --keep activé : dossier de travail conservé.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu.")
        sys.exit(130)