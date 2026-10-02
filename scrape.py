"""Pulls Placer County tax-default notices into county/placer-tax-default.csv.

Source: the Treasurer-Tax Collector's Public Notices page. Each notice is a PDF
with lines like:  008-000-000-000 OWNER NAME $1,234.56
Optional: set PARCELS_CSV_URL (repo variable) to the CSV download link of the
county open-data parcel file, and each parcel number gets its property address.
"""
import csv, datetime, io, os, re, sys, urllib.request

import pdfplumber

BASE = "https://www.placer.ca.gov"
INDEX = BASE + "/8975/Public-Notices"
OUT = "county/placer-tax-default.csv"
PARCELS_CSV_URL = os.environ.get("PARCELS_CSV_URL", "").strip()
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


def parcel_addresses():
    """Returns {apn digits: (address, city, zip)} from the open-data parcel CSV."""
    if not PARCELS_CSV_URL:
        return {}
    text = get(PARCELS_CSV_URL).decode("utf-8-sig", "ignore")
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []

    def col(pattern, avoid=None):
        for c in cols:
            if re.search(pattern, c, re.I) and not (avoid and re.search(avoid, c, re.I)):
                return c
        return None

    c_apn = col(r"^apn|parcel")
    c_addr = col(r"situs|address", r"mail|city|zip")
    c_city = col(r"city|community", r"mail")
    c_zip = col(r"zip", r"mail")
    if not c_apn or not c_addr:
        print("Parcel file columns not recognized:", cols)
        return {}
    out = {}
    for r in reader:
        out[digits(r.get(c_apn))] = ((r.get(c_addr) or "").strip(),
                                     (r.get(c_city) or "").strip() if c_city else "",
                                     (r.get(c_zip) or "").strip() if c_zip else "")
    return out


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
        lookup = parcel_addresses()
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
            a = lookup.get(d) or lookup.get(d[:9]) or ("", "", "")
            w.writerow([r["APN"], r["Owner"], r["Amount"], a[0], a[1], a[2], r["Source"]])
    print("Wrote", len(rows), "rows to", OUT)


if __name__ == "__main__":
    main()
