"""
Ground-truth COE-Received owners (provided by the Marketing Lead).

Builds:
  data/coe_received_ground_truth.csv  -> email, counsellor, city  (de-duped)
  data/coe_received_ghl_status.csv    -> email, counsellor, city, ghl_pipeline,
                                         ghl_stage, ghl_status, total_payment,
                                         coe_in_ghl  (what GHL actually says)
Prints a counsellor x city COE summary.

City rule: Gurbir + Navneet = Melbourne; everyone else = Sydney.
Run:  .venv\\Scripts\\python.exe migration-dashboard\\scripts\\_coe_ground_truth.py
"""
from __future__ import annotations
import csv
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "migration_dashboard.duckdb"

MELBOURNE = {"gurbir", "gurbir singh", "navneet", "navneet kaur"}

# email <TAB> counsellor  (exactly as supplied)
RAW = """\
mehran123123@gmail.com\tWajahad
jawadali224400@gmail.com\tWajahad
homaayoonhassan0480@gmail.com\tWajahad
farooqrasheed25@gmail.com\tWajahad
ehsansunny5666@gmail.com\tKajal
navi.tung007@gmail.com\tKajal
hassansohail20@gmail.com\tWajahad
zainammar396@gmail.com\tWajahad
umairsaleem1026@gmail.com\tKajal
mohsinraza3324090@gmail.com\tKajal
nabeel.auth@gmail.com\tWajahad
ahsanzaib51214@gmail.com\tSaurab
zainmuhammad626@gmail.com\tKajal
hussainsyed1412@gmail.com\tKajal
nomihassan82@gmail.com\tSaurab
tehseenshah36@gmail.com\tNavneet
maazbinkaleem@gmail.com\tWajahad
mahal20kj@gmail.com\tNavneet
usamakalyar6071@gmail.com\tSaurab
kaleemullahbhutta10@gmail.com\tSaurab
nodirmaksudov@gmail.com\tSaurab
arhamarif06@gmail.com\tSaurab
ihsankhan2662@gmail.com\tKajal
sardar1232018@gmail.com\tSaurab
shahalidepar2@gmail.com\tSaurab
asadbekuzb2008@gmail.com\tSaurab
ajamesfelix.work@gmail.com\tKajal
ukatyal4@gmail.com\tNavneet
alih77200@gmail.com\tSaurab
sherazgujjar1555@gmail.com\tKajal
mianabrar0336@gmail.com\tKajal
usmanchadhar222@icloud.com\tSaurab
younasad422@gmail.com\tKajal
vikasror7818@gmail.com\tNavneet
hamzaiqbal10101@gmail.com\tKajal
huzaifaidrees90@gmail.com\tKajal
shubhamchopraaaa@gmail.com\tKajal
isabirkhan8131@gmail.com\tSaurab
muhammadbilal031477@gmail.com\tKajal
ss19@somaiya.edu\tKajal
shaharyarbutt1@gmail.com\tNavneet
akhuwaja007@hotmail.com\tWajahad
khannoman.pk.7888@gmail.com\tWajahad
rjee58698@gmail.com\tKajal
muhammadzararmujahid@gmail.com\tWajahad
abiya326@gmail.com\tWajahad
muhammaddurrabzafar@gmail.com\tKajal
mateesafi72@gmail.com\tWajahad
hassaanzafar6@gmail.com\tWajahad
muhammadfaizan6335@gmail.com\tWajahad
syedqamar1405@gmail.com\tWajahad
rainaslam19@gmail.com\tSaurab
taqihassan5011@gmail.com\tSaurab
saadasjadsadi@gmail.com\tSaurab
ys2240163@gmail.com\tSaurab
muhammadkareemchheena@gmail.com\tSaurab
jatinsainixi@gmail.com\tSaurab
zufeng.1207@gmail.com\tSaurab
ghulamhaiderk4@gmail.com\tSaurab
saadullahchadhar11@gmail.com\tSaurab
farhadhameed657@gmail.com\tSaurab
nabeelwarraich7@gmail.com\tWajahad
mustafakhalid02@gmail.com\tWajahad
indieleoofficial@gmail.com\tKajal
arbab3601@gmail.com\tWajahad
souravpayal66@gmail.com\tKajal
hasanbilal4336@gmail.com\tKajal
arhamzahid57@gmail.com\tKajal
asad2978536@gmail.com\tKajal
hassan47740@gmail.com\tKajal
rajaluqman044@gmail.com\tWajahad
alihaiderjafferi5@gmail.com\tWajahad
cheemaabdullah715@gmail.com\tWajahad
jakiafrinhhh@gmail.com\tWajahad
ammar.suhaib33@gmail.com\tWajahad
bharthdanial396@gmail.com\tWajahad
qasimrana095@gmail.com\tWajahad
sajjadalinsw@gmail.com\tWajahad
contactrovaib@gmail.com\tWajahad
zaidisyedtaha@gmail.com\tWajahad
Muhkashif115@gmail.com\tKajal
sameentahir99@gmail.com\tWajahad
vigneshnambiar99@gmail.com\tKajal
adwaith570@gmail.com\tKajal
rs303041@gmail.com\tNavneet
arslanmusafir@gmail.com\tKajal
shihab.sharar2017@gmail.com\tNavneet
usmanhaider10140@gmail.com\tKajal
mianusman116@gmail.com\tKajal
siddiquibilal085@gmail.com\tWajahad
am2334900@gmail.com\tWajahad
zulfiqartaimoor894@gmail.com\tKajal
abhisheksuresh000@gmail.com\tKajal
faysalkabir800@gmail.com\tNavneet
loveeshrathi3@gmail.com\tKajal
prandhawa713@gmail.com\tNavneet
pratiksingh145@gmail.com\tWajahad
aizazhussain182@gmail.com\tKajal
arshadalimaken720@gmail.com\tKajal
adeel65594@gmail.com\tWajahad
majidmanzoor07@gmail.com\tKajal
sanjanadilip2002@gmail.com\tWajahad
naickermalvina@gmail.com\tKajal
hassanmanj881@gmail.com\tKajal
asadcui22@gmail.com\tKajal
aqib0483@gmail.com\tKajal
imadmalal2@gmail.com\tKajal
ahmadwaraich736@gmail.com\tKajal
tasriful71@gmail.com\tKajal
kashifimran536@gmail.com\tWajahad
bajrawillow890@gmail.com\tSaurab
jagseerj432@gmail.com\tSaurab
adeel.saif1222@gmail.com\tSaurab
harrych9694@gmail.com\tSaurab
sohaibshahid722@yahoo.com\tSaurab
shery.amir99@gmail.com\tSaurab
muhammedzubairafzal@gmail.com\tSaurab
muhammadbilal5648@gmail.com\tSaurab
nazasma040@gmail.com\tSaurab
tamanna141414@gmail.com\tSaurab
jasminecheema27@gmail.com\tSaurab
ahmad.baloch4242@gmail.com\tSaurab
usamacheemamuhammad@gmail.com\tSaurab
majidalirandhawa2017@gmail.com\tSaurab
hsnrza064@gmail.com\tSaurab
hijabzahra541@gmail.com\tKajal
nehaasultana@gmail.com\tKajal
naghmanaaz85@gmail.com\tKajal
saeedarif401@gmail.com\tKajal
husnain.ahmad.0096@gmail.com\tKajal
Tauseefahmad3915@gmail.com\tWajahad
adnanhassantarar00@gmail.com\tWajahad
mian.usama1201@gmail.com\tWajahad
rormandeep842@gmail.com\tKajal
touqeerchaudry@yahoo.com\tWajahad
rana467shahbaz@gmail.com\tSaurab
aayizfarooq@outlook.com\tWajahad
nabeelsajid68@gmail.com\tWajahad
alyanali937@gmail.com\tWajahad
usamaashraf2424@gmail.com\tWajahad
mr.sufyanahmed194@gmail.com\tWajahad
ah9442273@gmail.com\tWajahad
salmancrk@gmail.com\tKajal
m.aliali616@icloud.com\tKajal
saad.rajpoot7268@icloud.com\tKajal
vemulapalliavinash525@gmail.com\tKajal
abdulbutt078@gmail.com\tWajahad
mohammadalimubasher@gmail.com\tWajahad
usamabajwa2515@gmail.com\tWajahad
mrdaniyal382@gmail.com\tWajahad
fahadsm898@gmail.com\tSaurab
mithunkrishnanm@gmail.com\tKajal
tasmiachirpy@gmail.com\tWajahad
shahidkhan755@yahoo.com\tWajahad
iftikharr42@gmail.com\tSaurab
payrasaad@gmail.com\tSaurab
faizansaleem5857@gmail.com\tSaurab
annukhan47@yahoo.com\tSaurab
sohaibali2912@gmail.com\tSaurab
subhimaqsod@gmail.com\tKajal
usama.sadiq1126@gmail.com\tKajal
muhammadtouseef900@gmail.com\tSaurab
abubakr2686@gmail.com\tSaurab
khokharabdullah999@gmail.com\tSaurab
usamach649@hotmail.com\tSaurab
danyalturikhan@gmail.com\tKajal
ibtisamhaider5@gmail.com\tKajal
armanshahzad2203@gmail.com\tKajal
siid123ck@gmail.com\tKajal
alihaidar401402@gmail.com\tKajal
nagrahamza966@gmail.com\tKajal
orangzaib7887@gmail.com\tKajal
ammarafzal026@gmail.com\tKajal
74aish@gmail.com\tKajal
ghulamasghar6982@gmail.com\tSaurab
anasrahim11@icloud.com\tSaurab
fadiirajpoot143@gmail.com\tSaurab
umairabdullah9751@gmail.com\tSaurab
zohaib25112002@gmail.com\tSaurab
jameelyasir154@gmail.com\tSaurab
swastikamjakhu@gmail.com\tSaurab
zaibaa.khan02@gmail.com\tSaurab
mumerhassan@outlook.com\tWajahad
withasim1040@gmail.com\tWajahad
chjavaid23@gmail.com\tWajahad
shahdevarsh823@gmail.com\tWajahad
hamzalatif753163@gmail.com\tWajahad
mustafaswat5226@gmail.com\tMinhaz
safransakib690@gmail.com\tMinhaz
aleeshahikram@gmail.com\tKajal
wahidraja1000@gmail.com\tKajal
sadaqat7128@gmail.com\tKajal
deoabhi7@gmail.com\tKajal
cadnanabbas456@gmail.com\tWajahad
muhammadahsan256033@gmail.com\tKajal
Umair.ali1092@outlook.com\tKajal
nabeelarif789@gmail.com\tWajahad
imranamjad358@gmail.com\tWajahad
zohaibyounas1900@gmail.com\tKajal
ch.qasim.warraich.007@gmail.com\tTurab
awaisch9209@gmail.com\tWajahad
tahirsherazi0130@gmail.com\tKajal
hamza.zahid1514@gmail.com\tKajal
najmalm1437@gmail.com\tKajal
shahzadmohd318@gmail.com\tKajal
daniyalrashid008@gmail.com\tTurab
walychaudhary@gmail.com\tSaurab
princenaqeeb2530@gmail.com\tWajahad
aamiruet79@gmail.com\tSaurab
m.junaidkhan31@gmail.com\tSaurab
qaziamir106@gmail.com\tSaurab
zeeshanshoukat473@gmail.com\tSaurab
naqashshan79@gmail.com\tSaurab
"""


def city_of(counsellor: str) -> str:
    return "Melbourne" if counsellor.strip().lower() in MELBOURNE else "Sydney"


def main() -> None:
    # parse + de-dup by email (keep first owner; flag conflicts)
    seen: dict[str, str] = {}
    conflicts: list[str] = []
    for line in RAW.strip().splitlines():
        if "\t" not in line:
            continue
        email, counsellor = (x.strip() for x in line.split("\t", 1))
        key = email.lower()
        if key in seen and seen[key].lower() != counsellor.lower():
            conflicts.append(f"{email}: {seen[key]} vs {counsellor}")
            continue
        seen.setdefault(key, counsellor)

    rows = [(email, seen[email], city_of(seen[email])) for email in seen]
    data_dir = ROOT / "data"

    gt_path = data_dir / "coe_received_ground_truth.csv"
    with gt_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["email", "counsellor", "city"])
        w.writerows(sorted(rows, key=lambda r: (r[2], r[1], r[0])))
    print(f"Wrote {gt_path}  ({len(rows)} unique emails)")
    if conflicts:
        print(f"  NOTE: {len(conflicts)} emails had >1 owner (kept first):")
        for c in conflicts:
            print("   ", c)

    # summary counsellor x city
    print("\n=== COE Received by counsellor / city (ground truth) ===")
    summ: dict[tuple[str, str], int] = {}
    for _e, c, city in rows:
        summ[(city, c)] = summ.get((city, c), 0) + 1
    for (city, c), n in sorted(summ.items(), key=lambda kv: (kv[0][0], -kv[1])):
        print(f"  {city:<10} {c:<10} {n}")
    by_city: dict[str, int] = {}
    for (city, _c), n in summ.items():
        by_city[city] = by_city.get(city, 0) + n
    print("  ----")
    for city, n in by_city.items():
        print(f"  {city:<10} TOTAL      {n}")
    print(f"  {'GRAND':<10} TOTAL      {len(rows)}")

    # GHL status per email
    con = duckdb.connect(str(DB_PATH), read_only=True)
    emails = [e for e in seen]  # lowercased
    con.execute("CREATE TEMP TABLE _gt(email VARCHAR)")
    con.executemany("INSERT INTO _gt VALUES (?)", [(e,) for e in emails])
    q = """
    WITH latest_opp AS (
        SELECT o.contact_id, p.pipeline_name, s.stage_name, o.status,
               ROW_NUMBER() OVER (PARTITION BY o.contact_id ORDER BY o.created_at DESC) rn
        FROM fact_opportunities o
        LEFT JOIN dim_pipelines p ON p.pipeline_id = o.pipeline_id
        LEFT JOIN dim_stages   s ON s.stage_id    = o.stage_id
    ),
    pay AS (SELECT contact_id, SUM(amount - COALESCE(amount_refunded,0)) total_payment
            FROM fact_payments WHERE LOWER(status)='succeeded' GROUP BY contact_id),
    coe AS (SELECT DISTINCT o.contact_id FROM fact_opportunities o
            JOIN dim_pipelines p ON p.pipeline_id=o.pipeline_id
            JOIN dim_stages s ON s.stage_id=o.stage_id
            WHERE s.stage_name='COE Received'
              AND p.pipeline_name IN ('L2C - Education','CLT - Onshore Admission'))
    SELECT g.email,
           c.contact_id,
           lo.pipeline_name AS ghl_pipeline,
           lo.stage_name    AS ghl_stage,
           lo.status        AS ghl_status,
           COALESCE(pay.total_payment,0) AS total_payment,
           CASE WHEN coe.contact_id IS NOT NULL THEN 'yes' ELSE 'no' END AS coe_in_ghl
    FROM _gt g
    LEFT JOIN fact_contacts c ON LOWER(c.email) = g.email
    LEFT JOIN latest_opp lo   ON lo.contact_id = c.contact_id AND lo.rn = 1
    LEFT JOIN pay             ON pay.contact_id = c.contact_id
    LEFT JOIN coe             ON coe.contact_id = c.contact_id
    """
    df = con.execute(q).fetchdf()
    con.close()

    df["counsellor"] = df["email"].map(seen)
    df["city"] = df["counsellor"].map(city_of)
    out = df[["email", "counsellor", "city", "ghl_pipeline", "ghl_stage",
              "ghl_status", "total_payment", "coe_in_ghl"]].copy()
    st_path = data_dir / "coe_received_ghl_status.csv"
    out.to_csv(st_path, index=False, encoding="utf-8")
    print(f"\nWrote {st_path}")

    matched = int((df["contact_id"].notna()).sum())
    coe_yes = int((df["coe_in_ghl"] == "yes").sum())
    not_found = sorted(df.loc[df["contact_id"].isna(), "email"].tolist())
    print(f"\nGHL match: {matched}/{len(df)} emails found in the local DB.")
    print(f"  of matched, GHL shows COE Received: {coe_yes}")
    print(f"  not found in local DB: {len(not_found)}")
    print("\nWhere GHL currently has them (stage of their latest opp):")
    vc = df["ghl_stage"].fillna("(no opp / not found)").value_counts()
    for stage, n in vc.items():
        print(f"   {n:>4}  {stage}")


if __name__ == "__main__":
    main()
