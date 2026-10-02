"""Pulls Placer County tax-default notices into county/placer-tax-default.csv.

Source: the Treasurer-Tax Collector's Public Notices page. Each notice is a PDF
with lines like:  008-000-000-000 OWNER NAME $1,234.56
Each parcel number is then matched to its property address using the county's
public parcel layer (fields: Apn, SitusAddressFull, Acres).
"""
import csv, datetime, io, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

import pdfplumber

BASE = "https://www.placer.ca.gov"
INDEX = BASE + "/8975/Public-Notices"
OUT = "county/placer-tax-default.csv"
PARCELS = "https://services6.arcgis.com/PArfeTGcwA9RGNzN/ArcGIS/rest/services/Public_Parcels/FeatureServer/0"
UA = {"User-Agent": "Mozilla/5.0 (weekly public-notice reader)"}
DETAILS = "county/details.json"
RENTCAST_KEY = os.environ.get("RENTCAST_API_KEY", "").strip()
MONTHLY_CAP = 45  # the free RentCast plan includes 50 requests a month; stay under it
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
    """Returns {apn digits: (address, city, zip, acres)} for the wanted parcels.

    Pages through the county's public parcel layer (2,000 rows per request),
    reading only the parcel number, the full property address and the lot acreage.
    """
    out, offset = {}, 0
    while True:
        q = urllib.parse.urlencode({
            "where": "1=1", "outFields": "Apn,SitusAddressFull,Acres", "returnGeometry": "false",
            "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": 2000, "f": "json"})
        data = json.loads(get(PARCELS + "/query?" + q).decode("utf-8", "ignore"))
        if "error" in data:
            raise RuntimeError(data["error"])
        feats = data.get("features") or []
        for f in feats:
            a = f.get("attributes") or {}
            d = digits(a.get("Apn"))
            full = (a.get("SitusAddressFull") or "").strip()
            if d in wanted:
                acres = a.get("Acres")
                out[d] = split_address(full) + (("%.2f" % acres) if acres else "",)
        offset += len(feats)
        if not feats or not data.get("exceededTransferLimit"):
            break
    return out


CITIES = ["GRANITE BAY", "MEADOW VISTA", "TAHOE CITY", "KINGS BEACH", "TAHOE VISTA", "CARNELIAN BAY",
          "OLYMPIC VALLEY", "DUTCH FLAT", "GOLD RUN", "EMIGRANT GAP", "SODA SPRINGS", "PLEASANT GROVE",
          "CISCO GROVE", "AUBURN", "ROSEVILLE", "ROCKLIN", "LINCOLN", "LOOMIS", "COLFAX", "NEWCASTLE",
          "PENRYN", "FORESTHILL", "APPLEGATE", "WEIMAR", "SHERIDAN", "ALTA", "HOMEWOOD", "TAHOMA",
          "TRUCKEE", "NORDEN", "ELVERTA", "ANTELOPE", "BAXTER", "BOWMAN"]


def split_address(full):
    """'295 MAIN ST AUBURN CA 95603' or '295 MAIN ST, AUBURN, CA 95603' -> (street, city, zip)."""
    z = re.search(r"(\d{5})(?:-\d{4})?\s*$", full)
    zipc = z.group(1) if z else ""
    rest = re.sub(r"[\s,]*\d{5}(-\d{4})?\s*$", "", full)
    rest = re.sub(r"[\s,]+(CA|CALIFORNIA)\s*$", "", rest, flags=re.I).strip(" ,")
    if "," in rest:
        street, city = [p.strip() for p in rest.split(",", 1)]
        return street, city, zipc
    up = rest.upper()
    for c in CITIES:
        if up.endswith(" " + c):
            return rest[: -len(c)].strip(), rest[-len(c):].strip(), zipc
    return rest, "", zipc


def load_details():
    try:
        with open(DETAILS, encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("apns", {})
            return d
    except Exception:
        return {"month": "", "used": 0, "apns": {}}


def enrich(rows, lookup, store):
    """Fills beds, baths, size and year from the RentCast API, one request per property.

    Only runs when the RENTCAST_API_KEY secret is set. Each property is looked up once,
    highest back taxes first, and never more than MONTHLY_CAP requests in a calendar month.
    """
    month = datetime.date.today().strftime("%Y-%m")
    if store.get("month") != month:
        store["month"], store["used"] = month, 0
    if not RENTCAST_KEY:
        print("No RENTCAST_API_KEY set, skipping property details")
        return
    todo = [a for a in rows if a not in store["apns"] and (lookup.get(digits(a)) or ("",))[0]]
    todo.sort(key=lambda a: -float(rows[a]["Amount"].replace("$", "").replace(",", "") or 0))
    done = 0
    for apn in todo:
        if store["used"] >= MONTHLY_CAP:
            print("Monthly request cap reached")
            break
        street, city, zipc = lookup[digits(apn)][:3]
        address = ", ".join(x for x in [street, city, "CA", zipc] if x)
        url = "https://api.rentcast.io/v1/properties?" + urllib.parse.urlencode({"address": address})
        req = urllib.request.Request(url, headers={"X-Api-Key": RENTCAST_KEY, "Accept": "application/json"})
        store["used"] += 1
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as err:
            if err.code == 404:
                store["apns"][apn] = {}
                continue
            print("RentCast stopped the run with HTTP", err.code)
            break
        except Exception as err:
            print("RentCast request failed:", err)
            break
        p = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        store["apns"][apn] = {"beds": p.get("bedrooms"), "baths": p.get("bathrooms"),
                              "sqft": p.get("squareFootage"), "year": p.get("yearBuilt")}
        done += 1
        time.sleep(1)
    print("Property details looked up this run:", done, "| requests used this month:", store["used"])


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
        print(sum(1 for v in lookup.values() if v[0]), "of", len(rows), "parcels matched to an address")
    except Exception as err:
        print("Parcel file failed, continuing without addresses:", err)
        lookup = {}
    os.makedirs("county", exist_ok=True)
    store = load_details()
    enrich(rows, lookup, store)
    with open(DETAILS, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=1)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["APN", "Owner", "Amount", "Address", "City", "Zip", "Acres", "Beds", "Baths", "Sqft", "Year built", "Source"])
        for apn in sorted(rows):
            r = rows[apn]
            d = digits(apn)
            a = lookup.get(d) or ("", "", "", "")
            det = store["apns"].get(apn) or {}
            w.writerow([r["APN"], r["Owner"], r["Amount"], a[0], a[1], a[2], a[3],
                        det.get("beds") or "", det.get("baths") or "", det.get("sqft") or "", det.get("year") or "", r["Source"]])
    print("Wrote", len(rows), "rows to", OUT)


if __name__ == "__main__":
    main()
