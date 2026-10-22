#!/usr/bin/env python3
"""TÜİK TÜFE verisini çeker, doğrular ve docs/v1/tufe.json olarak yazar.

Yalnızca Python 3.12 standart kütüphanesi kullanılır.

Çıkış kodları:
    0  güncellendi, değişmedi ya da atlandı
    1  doğrulama hatası (dosya yazılmaz)
    2  ağ hatası
    3  gecikme (TÜİK verisi beklenen ayı içermiyor)
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATASET_URL = (
    "https://databrowser2.tuik.gov.tr/api/core/nodes/1/datasets/"
    "TR,DF_TUFE_SDMX_TT09,1.0"
)
STRUCTURE_URL = DATASET_URL + "/structure"
DATA_URL = DATASET_URL + "/data"
USER_AGENT = (
    "kisisel-enflasyonum-veri/1.0 "
    "(+https://github.com/F1R4VUN/kisisel-enflasyonum-veri)"
)
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": USER_AGENT,
    "Origin": "https://databrowser2.tuik.gov.tr",
    "Referer": "https://databrowser2.tuik.gov.tr/",
}
TIMEOUT = 120
RETRY_WAITS = (30, 90)  # 3 deneme: aralarda 30 ve 90 saniye

FILTERS = {
    "REF_AREA": "TR",
    "FREQ": "M",
    "DEGISIM": "1",
    "BASE_PER": "2025",
    "INDICATOR": "F_TFE",
}
CODE_DIM = "COICOP_2018"
TIME_DIM = "TIME_PERIOD"

HEADLINE = ("0", "01")
TIGHT_CODES = ("0", "01", "011", "012")
TIGHT_RATIO = (0.80, 1.30)
WIDE_RATIO = (0.40, 2.50)
BASE_YEAR = "2025"
BASE_TOLERANCE = 0.5
REVISION_TOLERANCE = 0.001
DELAY_DAY = 10

SCHEMA = 1
SOURCE = (
    "TÜİK, Tüketici Fiyat Endeksi, harcama gruplarına göre endeks sonuçları "
    "(2025=100)"
)
SOURCE_URL = "https://veriportali.tuik.gov.tr"
BASE_LABEL = "2025=100"
DEFAULT_FIRST_MONTH = "2024-01"
DEFAULT_NAMES = Path(__file__).resolve().parent / "names_tr.json"

EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_NETWORK = 2
EXIT_DELAY = 3

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ValidationError(Exception):
    """Veri ya da dosya beklenen biçimde değil."""


class NetworkError(Exception):
    """TÜİK'e erişilemedi ya da TÜİK hata döndürdü."""


# --- Ağ ---------------------------------------------------------------------

sleep = time.sleep


def http_request(method: str, url: str, body: bytes | None = None) -> tuple[int, bytes]:
    """Tek ağ çağrısı noktası; testler bunu sahtesiyle değiştirir.

    (durum kodu, gövde) döndürür. Bağlantı hatasında OSError yükseltir.
    """
    req = urllib.request.Request(url, data=body, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
        except (OSError, http.client.HTTPException):
            payload = b""
        return exc.code, payload
    except http.client.HTTPException as exc:
        raise OSError(f"{type(exc).__name__}: {exc}") from exc


def fetch(method: str, url: str, body: bytes | None = None) -> bytes:
    """Ağ hatası ve 5xx'te sınırlı sayıda yeniden dener; 4xx'te denemez."""
    attempts = len(RETRY_WAITS) + 1
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            status, payload = http_request(method, url, body)
        except OSError as exc:
            last = f"ağ hatası: {exc}"
        else:
            if 200 <= status < 300:
                return payload
            snippet = payload[:300].decode("utf-8", "replace").strip()
            last = f"HTTP {status}" + (f": {snippet}" if snippet else "")
            if status < 500:
                raise NetworkError(f"{method} {url} → {last}")
        if attempt < attempts:
            wait = RETRY_WAITS[attempt - 1]
            log(f"{method} {url} → {last}; {wait} sn sonra yeniden denenecek "
                f"({attempt}/{attempts})")
            sleep(wait)
    raise NetworkError(f"{method} {url} → {attempts} denemede başarısız, son hata: {last}")


def decode_json(payload: bytes, what: str) -> object:
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NetworkError(f"{what} JSON değil: {exc}") from exc


def criteria_from_structure(structure: object) -> list:
    try:
        criteria = structure["template"]["criteria"]
    except (TypeError, KeyError) as exc:
        raise ValidationError("Yapı yanıtında template.criteria yok") from exc
    if not isinstance(criteria, list):
        raise ValidationError("Yapı yanıtında template.criteria bir liste değil")
    return criteria


def load_dataset(args: argparse.Namespace) -> object:
    if args.input:
        return read_json(Path(args.input), "Girdi dosyası")
    if args.structure:
        structure = read_json(Path(args.structure), "Yapı dosyası")
    else:
        structure = decode_json(fetch("GET", STRUCTURE_URL), "Yapı yanıtı")
    body = json.dumps(criteria_from_structure(structure)).encode("utf-8")
    return decode_json(fetch("POST", DATA_URL, body), "Veri yanıtı")


# --- JSON-stat ayrıştırma ---------------------------------------------------

def category_order(dim_id: str, dimension: dict) -> list[str]:
    try:
        category = dimension["category"]
    except (TypeError, KeyError) as exc:
        raise ValidationError(f"{dim_id} boyutunda category yok") from exc
    index = category.get("index")
    if index is None:
        labels = category.get("label") or {}
        if len(labels) != 1:
            raise ValidationError(f"{dim_id} boyutunda index yok")
        return list(labels)
    if isinstance(index, list):
        return [str(c) for c in index]
    if isinstance(index, dict):
        return [str(c) for c, _ in sorted(index.items(), key=lambda kv: kv[1])]
    raise ValidationError(f"{dim_id} boyutunda index liste ya da sözlük değil")


def parse_number(raw: object, where: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValidationError(f"{where}: sayı değil: {raw!r}")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if text == "":
            return None
        try:
            return float(text)
        except ValueError:
            raise ValidationError(f"{where}: sayı değil: {raw!r}") from None
    raise ValidationError(f"{where}: sayı değil: {raw!r}")


def parse_jsonstat(ds: object) -> dict[str, dict[str, float]]:
    """JSON-stat 2.0 veri kümesini {kod: {ay: değer}} biçimine çevirir.

    Eksik değerler sözlüğe girmez.
    """
    if not isinstance(ds, dict):
        raise ValidationError("Veri yanıtı bir JSON nesnesi değil")
    ids, sizes = ds.get("id"), ds.get("size")
    dims = ds.get("dimension")
    if not isinstance(ids, list) or not isinstance(sizes, list) or len(ids) != len(sizes):
        raise ValidationError("Veri yanıtında id/size eksik ya da uyumsuz")
    if not isinstance(dims, dict):
        raise ValidationError("Veri yanıtında dimension yok")
    for required in (CODE_DIM, TIME_DIM):
        if required not in ids:
            raise ValidationError(f"Veri yanıtında {required} boyutu yok")

    categories = []
    for dim_id, size in zip(ids, sizes):
        cats = category_order(dim_id, dims.get(dim_id))
        if len(cats) != size:
            raise ValidationError(
                f"{dim_id} boyutunda {len(cats)} kategori var, size {size} diyor")
        categories.append(cats)

    allowed = []
    for dim_id, cats in zip(ids, categories):
        if dim_id in FILTERS:
            allowed.append({i for i, c in enumerate(cats) if c == FILTERS[dim_id]})
        else:
            allowed.append(None)

    total = math.prod(sizes)
    values = ds.get("value")
    if isinstance(values, dict):
        items = values.items()
    elif isinstance(values, list):
        if len(values) != total:
            raise ValidationError(
                f"value listesinde {len(values)} eleman var, beklenen {total}")
        items = enumerate(values)
    else:
        raise ValidationError("Veri yanıtında value yok")

    code_pos, time_pos = ids.index(CODE_DIM), ids.index(TIME_DIM)
    result: dict[str, dict[str, float]] = {}
    for key, raw in items:
        try:
            flat = int(key)
        except (TypeError, ValueError):
            raise ValidationError(f"value anahtarı sayı değil: {key!r}") from None
        if not 0 <= flat < total:
            raise ValidationError(f"value anahtarı aralık dışında: {key!r}")
        coords = [0] * len(sizes)
        rest = flat
        for d in range(len(sizes) - 1, -1, -1):  # son boyut en hızlı değişir
            rest, coords[d] = divmod(rest, sizes[d])
        if any(a is not None and c not in a for a, c in zip(allowed, coords)):
            continue
        code = categories[code_pos][coords[code_pos]]
        month = categories[time_pos][coords[time_pos]]
        value = parse_number(raw, f'"{code}" {month}')
        if value is None:
            continue
        if not MONTH_RE.match(month):
            raise ValidationError(f"Ay YYYY-MM biçiminde değil: {month!r}")
        series = result.setdefault(code, {})
        if month in series and series[month] != value:
            raise ValidationError(
                f'"{code}" {month} iki kez farklı değerle geldi: '
                f"{series[month]} ve {value}")
        series[month] = value
    return result


# --- Aylar ------------------------------------------------------------------

def month_range(first: str, last: str) -> list[str]:
    year, month = int(first[:4]), int(first[5:])
    out = []
    while True:
        current = f"{year:04d}-{month:02d}"
        if current > last:
            return out
        out.append(current)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def previous_month(day: dt.date) -> str:
    if day.month == 1:
        return f"{day.year - 1:04d}-12"
    return f"{day.year:04d}-{day.month - 1:02d}"


# --- Çıktı nesnesi ----------------------------------------------------------

def build_document(obs: dict[str, dict[str, float]], names: dict[str, str],
                   first_month: str) -> dict:
    if not obs.get("01"):
        raise ValidationError('"01" serisinde hiç değer yok')
    latest = max(obs["01"])
    if latest < first_month:
        raise ValidationError(
            f'"01" serisinin son ayı ({latest}) başlangıç ayından ({first_month}) önce')
    months = month_range(first_month, latest)
    series = {}
    for code in names:
        values = obs.get(code, {})
        series[code] = [
            round(values[m], 6) if m in values else None for m in months
        ]
    return {
        "schema": SCHEMA,
        "source": SOURCE,
        "sourceUrl": SOURCE_URL,
        "base": BASE_LABEL,
        "firstMonth": first_month,
        "latestMonth": latest,
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "names": dict(names),
        "series": series,
    }


def dumps_document(doc: dict) -> str:
    """Okunaklı ve deterministik biçim: her seri tek satırda."""
    def enc(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(", ", ": "))

    def block(mapping: dict) -> str:
        lines = [f"    {enc(k)}: {enc(v)}" for k, v in mapping.items()]
        return "{\n" + ",\n".join(lines) + "\n  }"

    parts = []
    for key, value in doc.items():
        text = block(value) if isinstance(value, dict) else enc(value)
        parts.append(f"  {enc(key)}: {text}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def without_generated(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "generatedAt"}


# --- Doğrulama --------------------------------------------------------------

def short_list(items: list[str], limit: int = 6) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" … (+{len(items) - limit})" if len(items) > limit else "")


def validate(doc: dict, obs: dict[str, dict[str, float]], expected_latest: str,
             old: dict | None) -> tuple[list[str], list[str]]:
    """(sert hatalar, uyarılar) döndürür."""
    errors: list[str] = []
    warnings: list[str] = []
    latest = doc["latestMonth"]
    months = month_range(doc["firstMonth"], latest)

    for code, values in doc["series"].items():
        filled = [i for i, v in enumerate(values) if v is not None]

        # 1. Ana seriler eksiksiz olmalı.
        if code in HEADLINE:
            missing = [months[i] for i, v in enumerate(values) if v is None]
            if values[-1] is None:
                errors.append(f'"{code}" serisinin {latest} değeri yok')
            if missing:
                errors.append(f'"{code}" serisinde eksik aylar: {short_list(missing)}')

        # 2. Her beklenen kodun en az bir değeri olmalı.
        if not filled:
            errors.append(f'"{code}" kodunun {doc["firstMonth"]} sonrasında hiç değeri yok')
            continue

        # 3. Değerler sonlu ve pozitif.
        bad = [f"{months[i]}={values[i]}" for i in filled
               if not math.isfinite(values[i]) or values[i] <= 0]
        if bad:
            errors.append(f'"{code}" serisinde geçersiz değerler: {short_list(bad)}')

        # 4. Ardışık iki dolu ay arasındaki oran.
        low, high = TIGHT_RATIO if code in TIGHT_CODES else WIDE_RATIO
        for i in range(1, len(values)):
            prev, cur = values[i - 1], values[i]
            if prev is None or cur is None or prev <= 0 or cur <= 0:
                continue
            if not (math.isfinite(prev) and math.isfinite(cur)):
                continue
            ratio = cur / prev
            if not low <= ratio <= high:
                errors.append(
                    f'"{code}" {months[i - 1]}→{months[i]} oranı {ratio:.3f}, '
                    f"izin verilen [{low:.2f}, {high:.2f}]")

        # Uyarılar (ana seriler dışında).
        if code not in HEADLINE:
            if values[-1] is None:
                warnings.append(f'"{code}" kodunun {latest} değeri yok')
            gaps = [months[i] for i in range(filled[0], filled[-1]) if values[i] is None]
            if gaps:
                warnings.append(f'"{code}" serisinde boşluk: {short_list(gaps)}')

    # 5. Baz kontrolü: 2025'in 12 ayı dolu her kodda ortalama 100 ± 0,5.
    base_months = [f"{BASE_YEAR}-{m:02d}" for m in range(1, 13)]
    for code in doc["series"]:
        values = obs.get(code, {})
        if all(m in values for m in base_months):
            mean = sum(values[m] for m in base_months) / 12
            if not abs(mean - 100) <= BASE_TOLERANCE:
                errors.append(
                    f'"{code}" kodunun {BASE_YEAR} ortalaması {mean:.3f}; '
                    f"100 ± {BASE_TOLERANCE} bekleniyordu (baz değişmiş olabilir)")

    # 6. Son ay ne gelecekte ne de eski dosyanın gerisinde olabilir.
    if latest > expected_latest:
        errors.append(f"Son ay {latest}, en fazla {expected_latest} olabilir")
    if old is not None and latest < old["latestMonth"]:
        errors.append(
            f"Son ay {latest}, eski dosyadaki {old['latestMonth']} değerinin gerisinde")

    # 7. Revizyon: eski dosyadaki değerler değişmemiş olmalı.
    if old is not None:
        new_months = {m: i for i, m in enumerate(months)}
        old_months = month_range(old["firstMonth"], old["latestMonth"])
        for code, old_values in old["series"].items():
            new_values = doc["series"].get(code)
            if new_values is None:
                continue
            changed = []
            for month, old_value in zip(old_months, old_values):
                i = new_months.get(month)
                if i is None or old_value is None or new_values[i] is None:
                    continue
                if old_value <= 0:
                    continue
                if abs(new_values[i] / old_value - 1) > REVISION_TOLERANCE:
                    changed.append(f"{month}: {old_value} → {new_values[i]}")
            if changed:
                errors.append(f'"{code}" geçmiş değerleri değişmiş: {short_list(changed)}')

    return errors, warnings


# --- Dosyalar ---------------------------------------------------------------

def read_json(path: Path, what: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(f"{what} okunamadı ({path}): {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{what} geçerli JSON değil ({path}): {exc}") from exc


def load_names(path: Path) -> dict[str, str]:
    names = read_json(path, "Ad dosyası")
    if not isinstance(names, dict) or not names:
        raise ValidationError(f"Ad dosyası boş ya da sözlük değil: {path}")
    for code in HEADLINE:
        if code not in names:
            raise ValidationError(f'Ad dosyasında "{code}" kodu yok: {path}')
    return names


def load_old(path: Path) -> dict | None:
    if not path.exists():
        return None
    old = read_json(path, "Eski çıktı dosyası")
    try:
        first, latest, series = old["firstMonth"], old["latestMonth"], old["series"]
        valid = (MONTH_RE.match(first) and MONTH_RE.match(latest)
                 and isinstance(series, dict))
    except (TypeError, KeyError):
        valid = False
    if not valid:
        raise ValidationError(f"Eski çıktı dosyası beklenen biçimde değil: {path}")
    length = len(month_range(first, latest))
    for code, values in series.items():
        if not isinstance(values, list) or len(values) != length:
            raise ValidationError(f'Eski çıktı dosyasında "{code}" serisinin uzunluğu hatalı')
    return old


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# --- Raporlama --------------------------------------------------------------

def log(message: str) -> None:
    print(message, flush=True)


def escape_annotation(message: str) -> str:
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notices: list[str] = []
        self.status = ""
        self.series_count = 0

    def _emit(self, kind: str, message: str) -> None:
        log(f"::{kind} title=TÜFE::{escape_annotation(message)}")

    def error(self, message: str) -> None:
        self.errors.append(message)
        self._emit("error", message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)
        self._emit("warning", message)

    def notice(self, message: str) -> None:
        self.notices.append(message)
        self._emit("notice", message)


def write_github_output(state: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"changed={'true' if state['changed'] else 'false'}\n")
        fh.write(f"latest={state['latest']}\n")
        fh.write(f"skipped={'true' if state['skipped'] else 'false'}\n")


def write_step_summary(report: Report, state: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "### TÜFE verisi",
        "",
        f"- Durum: {report.status or 'bilinmiyor'}",
        f"- Son ay: {state['latest'] or '—'}",
        f"- Seri sayısı: {report.series_count}",
        f"- Uyarılar: {len(report.warnings)}",
        f"- Hatalar: {len(report.errors)}",
    ]
    for title, items in (("Hatalar", report.errors), ("Uyarılar", report.warnings)):
        if items:
            lines += ["", f"#### {title}", ""] + [f"- {item}" for item in items]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\n")


# --- Ana akış ---------------------------------------------------------------

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="çıktı dosyası (docs/v1/tufe.json)")
    parser.add_argument("--force", action="store_true",
                        help="güncel olsa da çek; gecikme alarmını kapat")
    parser.add_argument("--dry-run", action="store_true",
                        help="doğrula ve özetle, dosya yazma")
    parser.add_argument("--input", help="ağ yerine yerel JSON-stat dosyası")
    parser.add_argument("--structure", help="ağ yerine yerel yapı dosyası")
    parser.add_argument("--names", default=str(DEFAULT_NAMES), help="kod → Türkçe ad dosyası")
    parser.add_argument("--first-month", default=DEFAULT_FIRST_MONTH, help="YYYY-MM")
    parser.add_argument("--today", help="YYYY-MM-DD (varsayılan: UTC'de bugün)")
    args = parser.parse_args(argv)
    if not MONTH_RE.match(args.first_month):
        parser.error("--first-month YYYY-MM biçiminde olmalı")
    if args.today:
        try:
            args.today = dt.date.fromisoformat(args.today)
        except ValueError:
            parser.error("--today YYYY-MM-DD biçiminde olmalı")
    else:
        args.today = dt.datetime.now(dt.timezone.utc).date()
    return args


def run(args: argparse.Namespace, report: Report, state: dict) -> int:
    out = Path(args.out)
    expected_latest = previous_month(args.today)
    names = load_names(Path(args.names))
    old = load_old(out)

    # Atlama yalnızca ağdan çekilecekken uygulanır; --input ile hep doğrulanır.
    if (old is not None and not args.force and not args.input
            and old["latestMonth"] == expected_latest):
        state["skipped"] = True
        state["latest"] = old["latestMonth"]
        report.status = "Atlandı (güncel)"
        report.series_count = len(old["series"])
        report.notice(f"Güncel, istek yapılmadı (son ay {old['latestMonth']}).")
        return EXIT_OK

    obs = parse_jsonstat(load_dataset(args))
    doc = build_document(obs, names, args.first_month)
    report.series_count = len(doc["series"])
    errors, warnings = validate(doc, obs, expected_latest, old)
    for message in warnings:
        report.warning(message)
    if errors:
        for message in errors:
            report.error(message)
        report.status = "Doğrulama hatası, dosya yazılmadı"
        return EXIT_VALIDATION

    latest = doc["latestMonth"]
    state["latest"] = latest
    extra = sorted(set(obs) - set(names))
    if extra:
        log(f"Ad dosyasında olmayan {len(extra)} kod yok sayıldı: {short_list(extra)}")

    if old is not None and without_generated(old) == without_generated(doc):
        report.status = "Değişiklik yok"
    elif args.dry_run:
        report.status = "Deneme (dosya yazılmadı)"
    else:
        write_atomic(out, dumps_document(doc))
        state["changed"] = True
        report.status = "Güncellendi"

    report.notice(
        f"{report.status}: son ay {latest}, {len(doc['series'])} seri, "
        f"{len(warnings)} uyarı.")

    if (not args.force and args.today.day >= DELAY_DAY and latest < expected_latest):
        report.error(f"TÜİK verisi gecikti: beklenen {expected_latest}, gelen {latest}")
        return EXIT_DELAY
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = Report()
    state = {"changed": False, "latest": "", "skipped": False}
    try:
        code = run(args, report, state)
    except ValidationError as exc:
        report.error(str(exc))
        report.status = "Doğrulama hatası, dosya yazılmadı"
        code = EXIT_VALIDATION
    except NetworkError as exc:
        report.error(str(exc))
        report.status = "Ağ hatası"
        code = EXIT_NETWORK
    write_github_output(state)
    write_step_summary(report, state)
    return code


if __name__ == "__main__":
    sys.exit(main())
