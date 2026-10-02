"""Pulls Placer County tax-default notices into county/placer-tax-default.csv.

Source: the Treasurer-Tax Collector's Public Notices page. Each notice is a PDF
with lines like:  008-000-000-000 OWNER NAME $1,234.56
Each parcel number is then matched to its property address using the county's
public parcel layer (fields: Apn, SitusAddressFull).
"""
import csv, datetime, io, json, os, re, sys, urllib.parse, urllib.request

import pdfplumber

BASE = "https://www.placer.ca.gov"
INDEX = BASE + "/8975/Public-Notices"
OUT = "county/placer-tax-default.csv"
PARCELS = "https://services6.arcgis.com/PArfeTGcwA9RGNzN/ArcGIS/rest/services/Public_Parcels/FeatureServer/0"
UA = {"User-Agent": "Mozilla/5.0 (weekly public-notice reader)"}
ROW = re.compile(r"^(\d{3}-\d{3}-\d{3}-\d{3})\s+(.+?)\s+\$([\d,]+\.\d{2})\s*$")


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120).read()


def digits(v):
    return re.sub(r"\D", "", v or "")


def notice_links():
    html = get(INDEX).decode("utf-8", "ignore")
    links = sorted(set(re.findall(r"/DocumentCenter/View/\d+/[A-Za-z0-9\-_%]+", html)))
    wanted = [l for l in links if re.search(r"default|power-to-sell", l, re.I)]
    year = datetime.date.today().year
    for y in (year, year - 1):
        hit = [l for l in wanted if str(y) in l]
        if hit:
            return hit
    return []


def read_notice(link):
    found = {}
    with pdfplumber.open(io.BytesIO(get(BASE + link))) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                m = ROW.match(line.strip())
                if m:
                    found[m.group(1)] = {"APN": m.group(1), "Owner": m.group(2).strip(),
                                         "Amount": "$" + m.group(3), "Source": link.rsplit("/", 1)[-1]}
    return found


def parcel_addresses(wanted):
    """Returns {apn digits: (address, city, zip)} for the wanted parcels.

    Pages through the county's public parcel layer (2,000 rows per request),
    reading only the parcel number and the full property address.
    """
    out, offset = {}, 0
    while True:
        q = urllib.parse.urlencode({
            "where": "1=1", "outFields": "Apn,SitusAddressFull", "returnGeometry": "false",
            "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": 2000, "f": "json"})
        data = json.loads(get(PARCELS + "/query?" + q).decode("utf-8", "ignore"))
        if "error" in data:
            raise RuntimeError(data["error"])
        feats = data.get("features") or []
        for f in feats:
            a = f.get("attributes") or {}
            d = digits(a.get("Apn"))
            full = (a.get("SitusAddressFull") or "").strip()
            if d in wanted and full:
                out[d] = split_address(full)
        offset += len(feats)
        if not feats or not data.get("exceededTransferLimit"):
            break
    return out


def split_address(full):
    """'123 MAIN ST, LINCOLN, CA 95648' -> (street, city, zip); falls back to the whole string."""
    z = re.search(r"(\d{5})(?:-\d{4})?\s*$", full)
    zipc = z.group(1) if z else ""
    parts = [p.strip() for p in full.split(",") if p.strip()]
    if len(parts) >= 2:
        city = re.sub(r"\s+(CA|CALIFORNIA)?\s*\d{5}(-\d{4})?$", "", parts[1], flags=re.I).strip()
        if re.fullmatch(r"(CA)?\s*\d{5}(-\d{4})?", city, flags=re.I):
            city = ""
        return parts[0], city, zipc
    street = re.sub(r"\s+\d{5}(-\d{4})?$", "", full).strip()
    return street, "", zipc


def main():
    rows = {}
    for link in notice_links():
        try:
            got = read_notice(link)
            print(len(got), "rows from", link)
            rows.update(got)
        except Exception as err:  # one bad PDF should not stop the rest
            print("Skipped", link, err)
    if not rows:
        print("No rows found. Leaving the existing file untouched.")
        sys.exit(1)
    try:
        lookup = parcel_addresses({digits(a) for a in rows})
        print(len(lookup), "of", len(rows), "parcels matched to an address")
    except Exception as err:
        print("Parcel file failed, continuing without addresses:", err)
        lookup = {}
    os.makedirs("county", exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["APN", "Owner", "Amount", "Address", "City", "Zip", "Source"])
        for apn in sorted(rows):
            r = rows[apn]
            d = digits(apn)
            a = lookup.get(d) or ("", "", "")
            w.writerow([r["APN"], r["Owner"], r["Amount"], a[0], a[1], a[2], r["Source"]])
    print("Wrote", len(rows), "rows to", OUT)


if __name__ == "__main__":
    main()
