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
SOLAR = "county/solar.json"
REPORTS_PAGE = BASE + "/2175/Building-Permit-Processing-Fees-Reports"
ROSEVILLE_CSV = "https://data.roseville.ca.us/api/views/buxi-gsvq/rows.csv?accessType=DOWNLOAD"
REPORTS_PER_RUN = 40  # county monthly permit reports read per run; the rest catch up on later runs
SOLAR_WORDS = re.compile(r"solar|photovoltaic|\bpv\b", re.I)
PERMIT_REC = re.compile(r"([A-Z]{2,4}\d{2}-\d{4,6})(.{0,400}?)(\d{3}-\d{3}-\d{3}-\d{3})", re.S)
APN_RE = re.compile(r"\d{3}-\d{3}-\d{3}-\d{3}")
STREET_WORDS = {"STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "DRIVE": "DR", "ROAD": "RD", "COURT": "CT", "LANE": "LN",
                "BOULEVARD": "BLVD", "BL": "BLVD", "PLACE": "PL", "CIRCLE": "CIR", "CR": "CIR", "WAY": "WY",
                "PARKWAY": "PKWY", "PKY": "PKWY", "PW": "PKWY", "TERRACE": "TER", "NORTH": "N", "SOUTH": "S",
                "EAST": "E", "WEST": "W"}
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


def parcel_addresses(wanted, centroids=True):
    """Returns {apn digits: (address, city, zip, acres, map point)} for the wanted parcels.

    Pages through the county's public parcel layer (2,000 rows per request),
    reading only the parcel number, the full property address and the lot acreage.
    """
    out, offset = {}, 0
    while True:
        q = urllib.parse.urlencode({
            "where": "1=1", "outFields": "Apn,SitusAddressFull,Acres", "returnGeometry": "false",
            "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": 2000, "f": "json",
            **({"returnCentroid": "true", "outSR": "4326"} if centroids else {})})
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
                c = f.get("centroid") or {}
                point = (c["x"], c["y"]) if "x" in c and "y" in c else None
                out[d] = split_address(full) + (("%.2f" % acres) if acres else "", point)
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
    if up in CITIES:  # the county lists only a city for parcels with no street address
        return "", rest, zipc
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


CITY_LAYER = "https://services6.arcgis.com/PArfeTGcwA9RGNzN/ArcGIS/rest/services/City_Boundaries/FeatureServer/0"
CITY_NAMES = ["ROSEVILLE", "ROCKLIN", "LINCOLN", "AUBURN", "LOOMIS", "COLFAX"]


def city_polygons():
    """Returns [(city name, rings)] from the county's city-limits layer, in longitude/latitude."""
    q = urllib.parse.urlencode({"where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": "4326", "f": "json"})
    data = json.loads(get(CITY_LAYER + "/query?" + q).decode("utf-8", "ignore"))
    if "error" in data:
        raise RuntimeError(data["error"])
    out = []
    for f in data.get("features") or []:
        text = " ".join(str(v).upper() for v in (f.get("attributes") or {}).values() if isinstance(v, str))
        name = next((c for c in CITY_NAMES if c in text), None)
        rings = (f.get("geometry") or {}).get("rings")
        if name and rings:
            out.append((name.title(), rings))
    return out


def inside(point, rings):
    """Even-odd test: is the point inside the polygon made of these rings?"""
    x, y = point
    hit = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
            j = i
    return hit


def jurisdiction(point, cities):
    if not point or not cities:
        return ""
    return next((name for name, rings in cities if inside(point, rings)), "Unincorporated")


def norm_street(text):
    """'140 Cleveland Avenue, Roseville' -> '140 CLEVELAND AVE' so two sources can be compared."""
    out = []
    for w in re.sub(r"[^A-Z0-9 ]", " ", str(text or "").upper().split(",")[0]).split():
        if w in ("UNIT", "APT", "STE", "SUITE", "ROSEVILLE", "CA"):
            break
        out.append(STREET_WORDS.get(w, w))
    return " ".join(out)


def scan_report(text, found):
    """Adds {apn: note} for every solar permit row in one county 'Building Permits Issued' report."""
    hits = 0
    for m in PERMIT_REC.finditer(text):
        desc = " ".join(m.group(2).split())
        if SOLAR_WORDS.search(desc):
            date = re.search(r"\d{2}/\d{2}/\d{4}", desc)
            found.setdefault(m.group(3), "County permit " + m.group(1) + (", issued " + date.group(0) if date else ""))
            hits += 1
    if not hits:  # older report layouts: fall back to one row per line
        for line in text.splitlines():
            m = APN_RE.search(line)
            if m and SOLAR_WORDS.search(line[:m.start()]):
                found.setdefault(m.group(0), "County solar permit")
                hits += 1
    return hits


def county_solar(store):
    """Reads the county's monthly 'Building Permits Issued' reports (unincorporated areas only)."""
    html = get(REPORTS_PAGE).decode("utf-8", "ignore")
    amid = None
    for m in re.finditer(r"Building Permits Issued", html):
        link = re.search(r"Archive\.aspx\?AMID=(\d+)", html[m.end():m.end() + 1500], re.I)
        if link:
            amid = link.group(1)
    if not amid:
        all_ids = re.findall(r"Archive\.aspx\?AMID=(\d+)", html, re.I)
        amid = all_ids[-1] if all_ids else None
    if not amid:
        print("County permit report archive not found")
        return False
    listing = get(BASE + "/Archive.aspx?AMID=" + amid).decode("utf-8", "ignore")
    adids = list(dict.fromkeys(re.findall(r"Archive\.aspx\?ADID=(\d+)", listing, re.I)))
    todo = [a for a in adids if a not in store["done"]][:REPORTS_PER_RUN]
    print(len(adids), "county permit reports listed,", len(todo), "to read this run")
    for adid in todo:
        try:
            with pdfplumber.open(io.BytesIO(get(BASE + "/Archive.aspx?ADID=" + adid))) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            print("Report", adid, "solar permits:", scan_report(text, store["apns"]))
            store["done"].append(adid)
        except Exception as err:
            print("Skipped report", adid, err)
        time.sleep(1)
    left = [a for a in adids if a not in store["done"]]
    print("County permit reports still unread:", len(left))
    return bool(adids) and not left


def roseville_solar():
    """Returns ({house number: [(street, note)]}, {apn digits: note}) from Roseville's open permit data."""
    text = get(ROSEVILLE_CSV).decode("utf-8-sig", "ignore")
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    print("Roseville permit columns:", cols)

    def col(pattern):
        return next((c for c in cols if re.search(pattern, c, re.I)), None)

    c_addr, c_apn = col(r"address|location|site"), col(r"apn|parcel")
    c_no, c_date = col(r"permit.*(no|num|#|id)|record|case"), col(r"issue|date")
    skip = [c for c in cols if re.search(r"contractor|applicant|company|owner", c, re.I)]
    by_num, by_apn = {}, {}
    for r in reader:
        if not SOLAR_WORDS.search(" ".join(str(v) for k, v in r.items() if v and k not in skip)):
            continue
        note = "Roseville permit" + (" " + r[c_no] if c_no and r.get(c_no) else "") + \
               (", " + str(r[c_date])[:10] if c_date and r.get(c_date) else "")
        if c_apn and digits(r.get(c_apn)):
            by_apn[digits(r[c_apn])] = note
        street = norm_street(r.get(c_addr)) if c_addr else ""
        if street and street.split(" ")[0].isdigit():
            by_num.setdefault(street.split(" ")[0], []).append((street, note))
    print("Roseville solar permits found:", sum(len(v) for v in by_num.values()) or len(by_apn))
    return by_num, by_apn


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
    wanted = {digits(a) for a in rows}
    try:
        try:
            lookup = parcel_addresses(wanted)
        except Exception as err:
            print("Parcel lookup with map points failed, retrying without them:", err)
            lookup = parcel_addresses(wanted, centroids=False)
        print(sum(1 for v in lookup.values() if v[0]), "of", len(rows), "parcels matched to an address")
    except Exception as err:
        print("Parcel file failed, continuing without addresses:", err)
        lookup = {}
    try:
        cities = city_polygons()
        print("City limits loaded for:", sorted({c[0] for c in cities}))
    except Exception as err:
        print("City limits failed, jurisdiction left blank:", err)
        cities = []
    os.makedirs("county", exist_ok=True)
    store = load_details()
    enrich(rows, lookup, store)
    with open(DETAILS, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=1)
    solar = json.load(open(SOLAR, encoding="utf-8")) if os.path.exists(SOLAR) else {}
    solar.setdefault("done", [])
    solar.setdefault("apns", {})
    county_ok = False
    try:
        county_ok = county_solar(solar)
    except Exception as err:
        print("County solar permit check failed:", err)
    solar["complete"] = bool(county_ok)
    with open(SOLAR, "w", encoding="utf-8") as f:
        json.dump(solar, f, indent=1)
    try:
        ros_num, ros_apn = roseville_solar()
    except Exception as err:
        print("Roseville solar permit check failed:", err)
        ros_num, ros_apn = {}, {}

    def solar_note(apn, street, city, juris):
        if apn in solar["apns"]:
            return solar["apns"][apn]
        if digits(apn) in ros_apn:
            return ros_apn[digits(apn)]
        if street and (juris == "Roseville" or (not juris and city.upper() == "ROSEVILLE")):
            s = norm_street(street)
            for cand, note in ros_num.get(s.split(" ")[0], []):
                if cand == s or cand.startswith(s + " ") or s.startswith(cand + " "):
                    return note
        return ""

    ros_ok = bool(ros_num or ros_apn)
    flagged = clear = 0
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["APN", "Owner", "Amount", "Address", "City", "Zip", "Acres", "Beds", "Baths", "Sqft", "Year built", "Solar", "Solar note", "Jurisdiction", "Source"])
        for apn in sorted(rows):
            r = rows[apn]
            d = digits(apn)
            a = lookup.get(d) or ("", "", "", "", None)
            det = store["apns"].get(apn) or {}
            juris = jurisdiction(a[4], cities)
            note = solar_note(apn, a[0], a[1], juris)
            flagged += 1 if note else 0
            checked = a[0] and ((juris == "Roseville" and ros_ok) or (juris == "Unincorporated" and county_ok))
            status = "has" if note else ("none" if checked else "")
            clear += 1 if status == "none" else 0
            w.writerow([r["APN"], r["Owner"], r["Amount"], a[0], a[1], a[2], a[3],
                        det.get("beds") or "", det.get("baths") or "", det.get("sqft") or "", det.get("year") or "",
                        status, note, juris, r["Source"]])
    print("Wrote", len(rows), "rows to", OUT, "|", flagged, "with a solar permit,", clear, "checked with none found")


if __name__ == "__main__":
    main()
