"""The three committed AOIs: design choices, Overpass queries and every retrieved source.

Every `SourceRef` below was fetched in the session that wrote this file (2026-09-03); its
`sha256` is the digest of the bytes actually retrieved and `accessed_utc` the time of that
retrieval (see the matching `source_document` rows in `data/manifest.jsonl`). Nothing here is
recalled from memory: capacities, statuses and locations quote the cited page, and every
geometry that is not an OSM node/way is flagged `hand_digitised_approximate` with the
rectangle or coordinate the source states.

Source kinds: government and intergovernmental bodies (DoED, Nepal Customs/Immigration,
Canton Valais, ICIMOD, WSL/ETH) are `agency_official`. Operator pages are the only public
statement of a plant's owner and are recorded as `operator_statement`, which does not
qualify a `best` value; every `best` here is backed by a DoED register or ICIMOD. Press is
`press_report` and is only used for 2025 events (the Blatten evacuation, the Friendship
Bridge rebuild).
"""

# ruff: noqa: E501  (Overpass QL lines and quoted excerpts are kept verbatim)
from __future__ import annotations

from datetime import UTC, datetime

from serac.domain.common import AttributedEstimate, Range, SourceKind, SourceRef
from serac.domain.events import AssetType
from serac.domain.geo import AssetStatus
from serac.pipelines.aoi_build import AoiSpec, AssetSpec, StatedLocation, TransectSpec

RECORD_CREATED = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
FIXTURE_DATE = "2026-09-03"


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _dms_centre(lat_lo: str, lat_hi: str, lon_lo: str, lon_hi: str) -> tuple[float, float]:
    """Centre of a DoED licence rectangle given as D M S strings, rounded to 4 decimals."""

    def dms(s: str) -> float:
        d, m, sec = (float(x) for x in s.split())
        return d + m / 60.0 + sec / 3600.0

    lat = (dms(lat_lo) + dms(lat_hi)) / 2.0
    lon = (dms(lon_lo) + dms(lon_hi)) / 2.0
    return (round(lon, 4), round(lat, 4))


# --- Lhende Khola / Bhote Koshi / Trishuli ----------------------------------------------------

DOED_POWERPLANTS = SourceRef(
    id="doed-powerplants-gt1mw",
    kind=SourceKind.agency_official,
    title=(
        "Department of Electricity Development (Nepal): power plants above 1 MW in operation "
        "(licence, capacity, river, promoter, licence rectangle, COD)"
    ),
    url="https://www.doed.gov.np/pages/powerplantsmorethan1",
    year=2026,
    publisher="Government of Nepal, Ministry of Energy, Water Resources and Irrigation, DoED",
    accessed_utc=_utc("2026-09-03T10:24:05Z"),
    sha256="266bd0f3b1d7ee62196540fcdbdcf63937cf6506f8fe6d572bbeceb3afcd0a7c",
    content_type="text/html",
    licence="No licence stated on the page (Government of Nepal publication)",
    claims_supported=[
        "exposed_assets.rasuwagadhi-hep.capacity_mw",
        "exposed_assets.rasuwagadhi-hep.status",
        "exposed_assets.upper-trishuli-3a.capacity_mw",
        "exposed_assets.upper-trishuli-3a.status",
        "exposed_assets.chilime-hep.capacity_mw",
        "exposed_assets.chilime-hep.status",
        "exposed_assets.trishuli-hep.capacity_mw",
        "exposed_assets.trishuli-hep.status",
        "exposed_assets.devighat-hep.capacity_mw",
        "exposed_assets.devighat-hep.status",
        "exposed_assets.sanjen-hep.capacity_mw",
        "exposed_assets.sanjen-hep.status",
        "exposed_assets.sanjen-hep.geometry",
        "exposed_assets.sanjen-upper-hep.capacity_mw",
        "exposed_assets.sanjen-upper-hep.status",
        "exposed_assets.sanjen-upper-hep.geometry",
    ],
    excerpt=(
        "Rasuwagadhi | 111.000 | Bhotekoshi (Rasuwa) | Thuman,Timure | COD 2081-09-16; Upper "
        "Trishuli 3A | 60.000 | NEA | COD 2076-05-13; Chilime | 22.000 | COD 2065-05-07; Trishuli "
        "| 24.000; Devighat | 14.100; Sanjen | 42.500 | COD 2081-09-01; Upper Sanjen | 14.800"
    ),
    peer_reviewed=False,
)

DOED_CONSTRUCTION = SourceRef(
    id="doed-construction-licences-gt1mw",
    kind=SourceKind.agency_official,
    title=(
        "Department of Electricity Development (Nepal): generation licences above 1 MW "
        "(construction/generation licence register with licence rectangles)"
    ),
    url="https://www.doed.gov.np/pages/clhydromorethan1",
    year=2026,
    publisher="Government of Nepal, Ministry of Energy, Water Resources and Irrigation, DoED",
    accessed_utc=_utc("2026-09-03T10:17:48Z"),
    sha256="8cc0029f86b51daea16628d14bed85d6d9bd5148b2e4ccc82f75a6f15abcc797",
    content_type="text/html",
    licence="No licence stated on the page (Government of Nepal publication)",
    claims_supported=[
        "exposed_assets.upper-trishuli-3b.capacity_mw",
        "exposed_assets.upper-trishuli-3b.geometry",
        "exposed_assets.upper-trishuli-1.capacity_mw",
    ],
    excerpt=(
        "Upper Trishuli 3B | 37.000 | Trishuli | Lic 104 | 2070-04-27 | NEA | 27°59'12\"-28°01'21\" N, "
        "85°10'11\"-85°12'01\" E | Manakamana (Nuwakot); Upper Trishuli-1 | 216.000 | Trishuli | Lic "
        "209 | 2074-07-23 | Nepal Water & Energy Development Co. | Dhunche,Haku (Rasuwa)"
    ),
    peer_reviewed=False,
)

RGHPCL = SourceRef(
    id="rghpcl-about-project",
    kind=SourceKind.operator_statement,
    title="Rasuwagadhi Hydropower Company Limited (operator page): About the Project (111 MW)",
    url="https://rghpcl.com.np/about-the-project/",
    year=2026,
    publisher="Rasuwagadhi Hydropower Company Limited (Chilime Hydropower Company subsidiary)",
    accessed_utc=_utc("2026-09-03T10:17:53Z"),
    sha256="e75eeee21be682e928cdd48a7f3c91543ba14d7714083a15d643bd3dadaf3c81",
    content_type="text/html",
    licence="No licence stated (operator web page)",
    claims_supported=[
        "exposed_assets.rasuwagadhi-hep.capacity_mw",
        "exposed_assets.rasuwagadhi-hep.status",
    ],
    excerpt=(
        "Rasuwagadhi Hydroelectric Project /Plant (111MW) ... located in Gosaikunda Rural "
        "Municipality, ward no. 1 and 2 (Thuman and Timure) of Rasuwa district. The Commercial "
        "Operation Date (COD) of the project is 16 Poush 2081 (31 December 2024)."
    ),
    peer_reviewed=False,
)

CHILIME = SourceRef(
    id="chilime-company-home",
    kind=SourceKind.operator_statement,
    title="Chilime Hydropower Company Limited (operator page): home, 22.1 MW plant",
    url="https://www.chilime.com.np/en/",
    year=2026,
    publisher="Chilime Hydropower Company Limited (NEA 51 %)",
    accessed_utc=_utc("2026-09-03T10:17:54Z"),
    sha256="235b2a14b7f7dc5f0c457bc2647e6c40dcf2be2a7ac7efba2796080697d72f0b",
    content_type="text/html",
    licence="No licence stated (operator web page)",
    claims_supported=[
        "exposed_assets.chilime-hep.capacity_mw",
        "exposed_assets.chilime-hep.status",
    ],
    excerpt=(
        "Chilime owns and operates 22.1 MW power plant commissioned on August 25, 2003 and "
        "located in Rasuwa district, 133 km north of capital city Kathmandu."
    ),
    peer_reviewed=False,
)

SJCL = SourceRef(
    id="sjcl-about",
    kind=SourceKind.operator_statement,
    title="Sanjen Jalavidhyut Company Limited (operator page): About (42.5 MW and 14.8 MW)",
    url="https://sjcl.com.np/about/",
    year=2026,
    publisher="Sanjen Jalavidhyut Company Limited (Chilime Hydropower Company subsidiary)",
    accessed_utc=_utc("2026-09-03T10:17:57Z"),
    sha256="92f8064fc179dbac58b2b9d1e95f5fc7cf0a745d362298c7ed07343a3466560f",
    content_type="text/html",
    licence="No licence stated (operator web page)",
    claims_supported=[
        "exposed_assets.sanjen-hep.capacity_mw",
        "exposed_assets.sanjen-upper-hep.capacity_mw",
    ],
    excerpt=(
        "Sanjen (Upper) Hydroelectric Project (SUHEP): This 14.8 MW project is located in Simbu "
        "Village at an elevation of 2,187 meters ... Sanjen Hydroelectric Project (SHEP): With a "
        "capacity of 42.5 MW, the powerhouse is situated in Chilime Village at 1,745 meters"
    ),
    peer_reviewed=False,
)

NWEDC = SourceRef(
    id="nwedc-home",
    kind=SourceKind.operator_statement,
    title="Nepal Water & Energy Development Company (developer page): Upper Trishuli-1, 216 MW",
    url="https://nwedcpl.com/",
    year=2026,
    publisher="Nepal Water & Energy Development Company Pvt. Ltd (private developer)",
    accessed_utc=_utc("2026-09-03T10:17:55Z"),
    sha256="a0921bdfc08ae235909eb4739d51d426b2359ce9f7680bd790e5007175d61c07",
    content_type="text/html",
    licence="No licence stated (developer web page)",
    claims_supported=["exposed_assets.upper-trishuli-1.capacity_mw"],
    excerpt=(
        "Upper Trishuli - 1 HEP, 216 MW. Upper Trishuli-1 Hydroelectric Project is a ROR scheme "
        "in Trishuli River with 216 MW installed."
    ),
    peer_reviewed=False,
)

CUSTOMS_RASUWA = SourceRef(
    id="customs-office-rasuwa-contact",
    kind=SourceKind.agency_official,
    title="Customs Office Rasuwa (Department of Customs, Nepal): contact page, Timure, Rasuwa",
    url="https://rasuwa.customs.gov.np/contact-us/",
    year=2026,
    publisher="Government of Nepal, Ministry of Finance, Department of Customs",
    accessed_utc=_utc("2026-09-03T10:24:10Z"),
    sha256="7d0db0e66db0f9dcb2be8f34e3101245a2afc727ff228e8cd103ecf69d0b1e16",
    content_type="text/html",
    licence="No licence stated on the page (Government of Nepal publication)",
    claims_supported=["exposed_assets.rasuwagadhi-kerung-border-post.status"],
    excerpt="रसुवा भन्सार कार्यालय टिमुरे, रसुवा (Customs Office Rasuwa, Timure, Rasuwa)",
    peer_reviewed=False,
)

IMMIGRATION_RASUWAGADHI = SourceRef(
    id="immigration-office-rasuwagadhi",
    kind=SourceKind.agency_official,
    title="Immigration Office Rasuwagadhi (Department of Immigration, Nepal): home page",
    url="https://rasuwagadhi.immigration.gov.np/",
    year=2026,
    publisher="Government of Nepal, Ministry of Home Affairs, Department of Immigration",
    accessed_utc=_utc("2026-09-03T10:24:12Z"),
    sha256="0866297ca0b20e2ebf7dedfa1a52b5966aea1527f4e87b6be7f9f44d76f900d9",
    content_type="text/html",
    licence="No licence stated on the page (Government of Nepal publication)",
    claims_supported=["exposed_assets.rasuwagadhi-kerung-border-post.status"],
    excerpt="नेपाल सरकार गृह मन्त्रालय अध्यागमन विभाग अध्यागमन कार्यालय रसुवागढी, रसुवागढी, रसुवा",
    peer_reviewed=False,
)

REPUBLICA_BRIDGE = SourceRef(
    id="myrepublica-2025-12-friendship-bridge",
    kind=SourceKind.press_report,
    title="Friendship Bridge rebuilt, Rasuwagadhi border set to reopen (Republica, 26 Dec 2025)",
    url=(
        "https://myrepublica.nagariknetwork.com/news/"
        "miteri-bridge-rebuilt-rasuwagadhi-border-set-to-reopen-60-25.html"
    ),
    authors=["Tapendra Karki"],
    year=2025,
    publisher="Republica (Nagarik Network)",
    accessed_utc=_utc("2026-09-03T10:23:05Z"),
    sha256="2af6b744dccd587e78658f0348282d242a1d4a910362f2830daa0bc7812ef4b9",
    content_type="text/html",
    licence="Copyright Nagarik Network; press report",
    claims_supported=["exposed_assets.miteri-bridge.status"],
    excerpt=(
        "The reconstruction of the Friendship Bridge at Rasuwagadhi border crossing - washed "
        "away by devastating floods in the Lhende river along the Nepal-China border - has been "
        "completed (Dec 26)"
    ),
    peer_reviewed=False,
)

COMCAT_US7000TBWB = SourceRef(
    id="usgs-comcat-us7000tbwb",
    kind=SourceKind.usgs_comcat,
    title="USGS ComCat event us7000tbwb: M 5.2 Landslide - 55 km NW of Kodari, Nepal",
    url="https://earthquake.usgs.gov/fdsnws/event/1/query?eventid=us7000tbwb&format=geojson",
    year=2026,
    publisher="U.S. Geological Survey",
    accessed_utc=_utc("2026-09-03T09:45:08.458851Z"),
    sha256="1d8f20a0d4e183e02fdf2b09b3a2ff38ae0d8dc63da2ae602cd3fb599bfe2073",
    content_type="application/json",
    licence="US-PD",
    stored_copy="data/fixtures/usgs_comcat/us7000tbwb.geojson",
    claims_supported=["source_zone.geometry", "notes"],
    excerpt="geometry: Point [85.515, 28.271, 0]; title: M 5.2 Landslide - 55 km NW of Kodari, Nepal",
    peer_reviewed=False,
)

LHENDE_QUERY = """
[out:json][timeout:180];
(
  way["waterway"]["name"~"Trishuli|Trisuli|Bhote Koshi|Bhotekoshi|Lhende|Lende|Langtang Khola",i](27.75,84.95,28.45,85.65);
  way["waterway"]["name:en"~"Trishuli|Trisuli|Bhote Koshi|Bhotekoshi|Lhende|Lende|Langtang Khola",i](27.75,84.95,28.45,85.65);
  relation["waterway"]["name"~"Trishuli|Trisuli|Bhote Koshi|Bhotekoshi|Lhende|Lende|Langtang Khola",i]["name"!~"system",i](27.75,84.95,28.45,85.65);
  relation["waterway"]["name:en"~"Trishuli|Trisuli|Bhote Koshi|Bhotekoshi|Lhende|Lende|Langtang Khola",i]["name"!~"system",i](27.75,84.95,28.45,85.65);
  way["waterway"~"^(river|stream)$"](28.24,85.44,28.33,85.56);
  node["place"]["name"~"Timure|Syabru|Syafru|Syaphru|Syapru|स्याफ्रु|स्याब्रु|Betrawati|Galchhi|Galchi|Rasuwagadhi|Rasuwa Gadhi|Kerung|Gyirong|Trishuli|Trisuli|Dhunche|Kyanjin|Langtang|Mailung|Devighat|Chilime",i](27.75,84.95,28.45,85.65);
  node["place"]["name:en"~"Timure|Syabru|Syafru|Syaphru|Syapru|स्याफ्रु|स्याब्रु|Betrawati|Galchhi|Galchi|Rasuwagadhi|Rasuwa Gadhi|Kerung|Gyirong|Trishuli|Trisuli|Dhunche|Kyanjin|Langtang|Mailung|Devighat|Chilime",i](27.75,84.95,28.45,85.65);
  nwr["power"="plant"]["plant:source"!="solar"](27.75,84.95,28.45,85.65);
  nwr["man_made"="dam"](27.75,84.95,28.45,85.65);
  nwr["waterway"="dam"](27.75,84.95,28.45,85.65);
  nw["name"~"Miteri|Friendship Bridge|Rasuwagadhi|Rasuwa Gadhi|Kerung|Sanjen|Devighat",i](27.75,84.95,28.45,85.65);
  nw["name:en"~"Miteri|Friendship Bridge|Rasuwagadhi|Rasuwa Gadhi|Kerung|Sanjen|Devighat",i](27.75,84.95,28.45,85.65);
  nwr["barrier"="border_control"](27.75,84.95,28.45,85.65);
  nwr["amenity"="customs"](27.75,84.95,28.45,85.65);
  nwr["name"~"Hydropower|Hydro Power|Hydroelectric",i](27.75,84.95,28.45,85.65);
  nwr["name:en"~"Hydropower|Hydro Power|Hydroelectric",i](27.75,84.95,28.45,85.65);
);
out geom;
"""

_SANJEN_LONLAT = _dms_centre("28 11 00", "28 13 00", "85 16 30", "85 18 15")
_SANJEN_UPPER_LONLAT = _dms_centre("28 13 00", "28 14 25", "85 16 30", "85 18 15")
_UT3B_LONLAT = _dms_centre("27 59 12", "28 01 21", "85 10 11", "85 12 01")

_OSM_LHENDE = "osm-overpass-lhende-khola-trishuli"

LHENDE_KHOLA_TRISHULI = AoiSpec(
    id="lhende-khola-trishuli",
    name="Langtang Lirung - Lhende Khola - Bhote Koshi - Trishuli corridor",
    countries=("NP", "CN"),
    epsg=32645,
    source_zone_bbox=(85.51, 28.27, 85.53, 28.29),
    river_names=(
        "Lhende Khola (Lende Khola / 东林藏布)",
        "Bhote Koshi (Kyirong Tsangpo)",
        "Trishuli",
    ),
    overpass_query=LHENDE_QUERY,
    downstream_target=(84.65, 27.86),
    chainage_km=100.0,
    fixture_path=f"data/fixtures/osm/lhende-khola-trishuli_overpass_{FIXTURE_DATE}.json",
    fixture_retrieved_utc=_utc("2026-09-03T10:17:27Z"),
    transects=(
        TransectSpec(
            "rasuwagadhi-gyirong",
            "Rasuwagadhi (Nepal-China border, Lhende Khola-Bhote Koshi confluence)",
            osm_node_id=992955542,
        ),
        TransectSpec(
            "syabrubesi", "Syabrubesi (Langtang Khola confluence)", osm_node_id=14127413536
        ),
        TransectSpec("betrawati", "Betrawati", osm_node_id=268864226),
        TransectSpec("galchhi", "Galchhi", osm_node_id=279374592),
    ),
    assets=(
        AssetSpec(
            id="rasuwagadhi-hep",
            name="Rasuwagadhi Hydroelectric Project",
            asset_type=AssetType.hydropower_plant,
            status=AssetStatus.operational,
            source_refs=(DOED_POWERPLANTS.id, RGHPCL.id),
            osm_way_id=1552793903,
            positional_accuracy_m=100.0,
            capacity_mw=Range(
                low=111.0,
                high=111.0,
                best=111.0,
                unit="MW",
                source_refs=[DOED_POWERPLANTS.id, RGHPCL.id],
            ),
            notes=(
                "Geometry is the OSM way 'Rasuwagadhi Hydropower Dam' (headworks); the "
                "powerhouse is in Timure per the operator. Status 'operational' = COD 31 Dec 2024 "
                "(operator) / 2081-09-16 BS (DoED), i.e. before the 26 Aug 2026 event."
            ),
        ),
        AssetSpec(
            id="upper-trishuli-3a",
            name="Upper Trishuli 3A Hydropower Project",
            asset_type=AssetType.hydropower_plant,
            status=AssetStatus.operational,
            source_refs=(DOED_POWERPLANTS.id,),
            osm_way_id=1554017038,
            positional_accuracy_m=100.0,
            capacity_mw=Range(
                low=60.0, high=60.0, best=60.0, unit="MW", source_refs=[DOED_POWERPLANTS.id]
            ),
            notes="Geometry is the OSM way 'Upper Trishuli 3A Powerhouse'. DoED COD 2076-05-13 BS.",
        ),
        AssetSpec(
            id="chilime-hep",
            name="Chilime Hydropower Plant",
            asset_type=AssetType.hydropower_plant,
            status=AssetStatus.operational,
            source_refs=(DOED_POWERPLANTS.id, CHILIME.id),
            osm_node_id=982602383,
            positional_accuracy_m=100.0,
            capacity_mw=Range(
                low=22.0,
                high=22.1,
                best=22.0,
                unit="MW",
                source_refs=[DOED_POWERPLANTS.id, CHILIME.id],
                estimates=[
                    AttributedEstimate(
                        low=22.0, high=22.0, unit="MW", source_ref=DOED_POWERPLANTS.id
                    ),
                    AttributedEstimate(low=22.1, high=22.1, unit="MW", source_ref=CHILIME.id),
                ],
                notes="DoED licence register: 22.000 MW; operator: 22.1 MW installed.",
            ),
            notes=(
                "Geometry is the OSM node 'Chilime Hydropower Powerhouse' near Syabrubesi; the "
                "OSM way 'Chilime Hydropower Plant' (28.179 N, 85.308 E) marks the headworks. "
                "Commissioned 25 Aug 2003 (operator)."
            ),
        ),
        AssetSpec(
            id="trishuli-hep",
            name="Trishuli Hydropower Station",
            asset_type=AssetType.hydropower_plant,
            status=AssetStatus.operational,
            source_refs=(DOED_POWERPLANTS.id,),
            osm_node_id=3492358494,
            positional_accuracy_m=100.0,
            capacity_mw=Range(
                low=24.0, high=24.0, best=24.0, unit="MW", source_refs=[DOED_POWERPLANTS.id]
            ),
            notes="Geometry is an OSM power=generator node named 'Trishuli Hydropower Station'.",
        ),
        AssetSpec(
            id="devighat-hep",
            name="Devighat Hydropower Station",
            asset_type=AssetType.hydropower_plant,
            status=AssetStatus.operational,
            source_refs=(DOED_POWERPLANTS.id,),
            osm_way_id=338849229,
            positional_accuracy_m=100.0,
            capacity_mw=Range(
                low=14.1, high=14.1, best=14.1, unit="MW", source_refs=[DOED_POWERPLANTS.id]
            ),
            notes="Geometry is the OSM power=generator way named 'Devighat'.",
        ),
        AssetSpec(
            id="upper-trishuli-3b",
            name="Upper Trishuli 3B Hydropower Project",
            asset_type=AssetType.hydropower_plant,
            status=AssetStatus.unknown,
            source_refs=(DOED_CONSTRUCTION.id,),
            stated_location=StatedLocation(
                lon=_UT3B_LONLAT[0],
                lat=_UT3B_LONLAT[1],
                positional_accuracy_m=3000.0,
                basis=(
                    "centre of the DoED generation-licence rectangle 27°59'12\"-28°01'21\" N, "
                    "85°10'11\"-85°12'01\" E (no OSM feature)"
                ),
            ),
            capacity_mw=Range(
                low=37.0, high=37.0, best=37.0, unit="MW", source_refs=[DOED_CONSTRUCTION.id]
            ),
            notes=(
                "Holds a generation licence (NEA, 2070-04-27 BS); no retrieved source states "
                "whether it is under construction or operating, so status is unknown."
            ),
        ),
        AssetSpec(
            id="upper-trishuli-1",
            name="Upper Trishuli-1 Hydroelectric Project",
            asset_type=AssetType.hydropower_plant,
            status=AssetStatus.unknown,
            source_refs=(DOED_CONSTRUCTION.id, NWEDC.id),
            osm_way_id=1552864314,
            positional_accuracy_m=200.0,
            capacity_mw=Range(
                low=216.0,
                high=216.0,
                best=216.0,
                unit="MW",
                source_refs=[DOED_CONSTRUCTION.id, NWEDC.id],
            ),
            notes=(
                "Geometry is the OSM way 'Upper Trishuli-1 Hydroelectric Project' near Mailung, "
                "inside the DoED licence rectangle. Neither retrieved source states a commissioning "
                "date, so status is unknown. OSM construction ways named 'Upper Trishuli-3 "
                "Hydropower Project' (28.08-28.13 N) fall inside this licence rectangle and were "
                "not used."
            ),
        ),
        AssetSpec(
            id="sanjen-hep",
            name="Sanjen Hydroelectric Project",
            asset_type=AssetType.hydropower_plant,
            status=AssetStatus.operational,
            source_refs=(DOED_POWERPLANTS.id, SJCL.id),
            stated_location=StatedLocation(
                lon=_SANJEN_LONLAT[0],
                lat=_SANJEN_LONLAT[1],
                positional_accuracy_m=3000.0,
                basis=(
                    "centre of the DoED licence rectangle 28°11'-28°13' N, 85°16'30\"-85°18'15\" E; "
                    "operator: powerhouse in Chilime village at 1,745 m (no OSM feature)"
                ),
            ),
            capacity_mw=Range(
                low=42.5,
                high=42.5,
                best=42.5,
                unit="MW",
                source_refs=[DOED_POWERPLANTS.id, SJCL.id],
            ),
            notes="On the Sanjen Khola (tributary), not on the corridor centreline. DoED COD 2081-09-01 BS.",
        ),
        AssetSpec(
            id="sanjen-upper-hep",
            name="Sanjen (Upper) Hydroelectric Project",
            asset_type=AssetType.hydropower_plant,
            status=AssetStatus.operational,
            source_refs=(DOED_POWERPLANTS.id, SJCL.id),
            stated_location=StatedLocation(
                lon=_SANJEN_UPPER_LONLAT[0],
                lat=_SANJEN_UPPER_LONLAT[1],
                positional_accuracy_m=3000.0,
                basis=(
                    "centre of the DoED licence rectangle 28°13'-28°14'25\" N, 85°16'30\"-85°18'15\" E; "
                    "operator: powerhouse in Simbu village at 2,187 m (no OSM feature)"
                ),
            ),
            capacity_mw=Range(
                low=14.8,
                high=14.8,
                best=14.8,
                unit="MW",
                source_refs=[DOED_POWERPLANTS.id, SJCL.id],
            ),
            notes="On the Sanjen Khola (tributary), not on the corridor centreline. DoED COD 2080-06-21 BS.",
        ),
        AssetSpec(
            id="rasuwagadhi-kerung-border-post",
            name="Rasuwagadhi-Kerung (Gyirong) border post: customs and immigration",
            asset_type=AssetType.border_post,
            status=AssetStatus.unknown,
            source_refs=(CUSTOMS_RASUWA.id, IMMIGRATION_RASUWAGADHI.id),
            osm_node_id=992955542,
            positional_accuracy_m=300.0,
            notes=(
                "Located at the OSM 'Rasuwa Gadhi' hamlet node; the Customs Office Rasuwa states "
                "its address as Timure, ~3 km south. No retrieved source gives a post-August-2026 "
                "operating status."
            ),
        ),
        AssetSpec(
            id="miteri-bridge",
            name="Nepal-China Friendship (Miteri) Bridge, Rasuwagadhi",
            asset_type=AssetType.bridge,
            status=AssetStatus.unknown,
            source_refs=(REPUBLICA_BRIDGE.id,),
            osm_way_id=1552746560,
            positional_accuracy_m=50.0,
            notes=(
                "Geometry is the OSM way tagged 'Temporary Friendship Bridge in Rasuwagadhi'. "
                "Press (Republica, 26 Dec 2025) reports the bridge washed away by the July 2025 "
                "Lhende flood and rebuilt; no official confirmation and no post-August-2026 status "
                "were retrieved, so status is unknown."
            ),
        ),
        AssetSpec(
            id="timure",
            name="Timure",
            asset_type=AssetType.settlement,
            status=AssetStatus.unknown,
            source_refs=(),
            osm_node_id=2553894641,
            positional_accuracy_m=500.0,
            notes="OSM place=suburb node; population not sourced (null).",
        ),
        AssetSpec(
            id="syabrubesi",
            name="Syabrubesi (Shyaphru Bensi)",
            asset_type=AssetType.settlement,
            status=AssetStatus.unknown,
            source_refs=(),
            osm_node_id=14127413536,
            positional_accuracy_m=500.0,
            notes="OSM place=hamlet node; population not sourced (null).",
        ),
        AssetSpec(
            id="betrawati",
            name="Betrawati",
            asset_type=AssetType.settlement,
            status=AssetStatus.unknown,
            source_refs=(),
            osm_node_id=268864226,
            positional_accuracy_m=500.0,
            notes="OSM place=hamlet node; population not sourced (null).",
        ),
    ),
    sources=(
        COMCAT_US7000TBWB,
        DOED_POWERPLANTS,
        DOED_CONSTRUCTION,
        RGHPCL,
        CHILIME,
        SJCL,
        NWEDC,
        CUSTOMS_RASUWA,
        IMMIGRATION_RASUWAGADHI,
        REPUBLICA_BRIDGE,
    ),
    extent_source_refs=(COMCAT_US7000TBWB.id,),
    notes=(
        "DESIGN CHOICES: the source zone is a rectangle 85.51-85.53 E, 28.27-28.29 N chosen so "
        "that the USGS ComCat epicentre of us7000tbwb (85.515 E, 28.271 N, fixture "
        "data/fixtures/usgs_comcat/us7000tbwb.geojson) lies inside it; it is not a mapped scar. "
        "The corridor follows the OSM Lhende Khola (Lende Khola / 东林藏布) to its confluence with "
        "the Bhote Koshi at Rasuwagadhi, then the Bhote Koshi and Trishuli past Galchhi; the "
        "downstream target (84.65 E, 27.86 N) is on the Trishuli west of Galchhi and the "
        "centreline is clipped to 100 km chainage. In OSM the Lhende Khola is a Tibetan tributary "
        "joining at Rasuwagadhi; the unnamed streams mapped inside the source zone are not "
        "connected to it in OSM, so chainage 0 is on the nearest connected reach (offset "
        "reported in BUILD). STATUS CAVEAT: asset statuses are as of the cited sources "
        "(pre-26 Aug 2026); impacts of the event belong to the event record, not here. "
        "ASSETS: every requested asset has a retrieved source; none was omitted as "
        "'seen in press only'. Sanjen, Sanjen (Upper) and Upper Trishuli 3B have no OSM feature "
        "and are placed at the centre of their DoED licence rectangles (approximate, ±3 km). "
        "Settlement populations are null (no source retrieved)."
    ),
    record_created_utc=RECORD_CREATED,
)

# --- Chamoli / Rishiganga ---------------------------------------------------------------------

SHUGAR_2021 = SourceRef(
    id="shugar-2021-crossref",
    kind=SourceKind.peer_reviewed,
    title="A massive rock and ice avalanche caused the 2021 disaster at Chamoli, Indian Himalaya",
    url="https://api.crossref.org/works/10.1126/science.abh4455",
    doi="10.1126/science.abh4455",
    authors=["Shugar, D. H.", "Jacquemart, M.", "Shean, D.", "Bhushan, S.", "et al."],
    year=2021,
    publisher="Science (AAAS); Crossref metadata record",
    accessed_utc=_utc("2026-09-03T10:17:50Z"),
    sha256="2b58e6743a6f3677421bc1af7491533ab487e564e1b2eb966742e874cf4949f1",
    content_type="application/json",
    licence="Crossref metadata; article copyright AAAS (paywalled; landing page not retrievable)",
    claims_supported=["source_zone.geometry", "river_names"],
    excerpt="Crossref: Science, issued 2021-07-16, DOI 10.1126/science.abh4455, authors Shugar, Jacquemart, Shean, Bhushan ...",
    peer_reviewed=True,
)

SHUGAR_2021_ABSTRACT = SourceRef(
    id="shugar-2021-abstract-europepmc",
    kind=SourceKind.peer_reviewed,
    title="Shugar et al. 2021, abstract as indexed by Europe PMC (PMID 34112725)",
    url=(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
        "query=DOI:%2210.1126/science.abh4455%22&format=json&resultType=core"
    ),
    doi="10.1126/science.abh4455",
    authors=["Shugar, D. H.", "et al."],
    year=2021,
    publisher="Europe PMC (abstract record)",
    accessed_utc=_utc("2026-09-03T10:17:51Z"),
    sha256="277fac2f62ab2724e4ee88a9e277b9708b16e7617cb5a4d6e84b95c58b8b7bfc",
    content_type="application/json",
    licence="Europe PMC abstract record; article copyright AAAS",
    claims_supported=[
        "source_zone.geometry",
        "river_names",
        "exposed_assets.rishiganga-hep.status",
        "exposed_assets.tapovan-vishnugad-hep.status",
    ],
    excerpt=(
        "a catastrophic mass flow descended the Ronti Gad, Rishiganga, and Dhauliganga valleys in "
        "Chamoli ... severely damaging two hydropower projects ... ~27 x 10^6 cubic meters of rock "
        "and glacier ice collapsed from the steep north face of Ronti Peak."
    ),
    peer_reviewed=True,
)

COOK_2021 = SourceRef(
    id="cook-2021-crossref",
    kind=SourceKind.peer_reviewed,
    title=(
        "Detection and potential early warning of catastrophic flow events with regional "
        "seismic networks"
    ),
    url="https://api.crossref.org/works/10.1126/science.abj1227",
    doi="10.1126/science.abj1227",
    authors=["Cook, K. L.", "Rekapalli, R.", "Dietze, M.", "et al."],
    year=2021,
    publisher="Science (AAAS); Crossref metadata record",
    accessed_utc=_utc("2026-09-03T10:17:51Z"),
    sha256="eddf744afcf42cbed08e61a29c7682b40b471a135ff086954785fcb25eff3291",
    content_type="application/json",
    licence="Crossref metadata; article copyright AAAS (paywalled; landing page not retrievable)",
    claims_supported=["notes"],
    excerpt="Crossref: Science, issued 2021-10, DOI 10.1126/science.abj1227, authors Cook, Rekapalli, Dietze ...",
    peer_reviewed=True,
)

ICIMOD_CHAMOLI = SourceRef(
    id="icimod-2021-chamoli-flood",
    kind=SourceKind.agency_official,
    title=(
        "Understanding the Chamoli flood: cause, process, impacts, and context of rapid "
        "infrastructure development (ICIMOD, 2021)"
    ),
    url=(
        "https://www.icimod.org/article/understanding-the-chamoli-flood-cause-process-impacts-"
        "and-context-of-rapid-infrastructure-development/"
    ),
    year=2021,
    publisher="International Centre for Integrated Mountain Development (ICIMOD)",
    accessed_utc=_utc("2026-09-03T10:22:54Z"),
    sha256="c0a9985a1b041309b7933f5f2d6d29e333b1ae635e37b1585a6136190cff2c67",
    content_type="text/html",
    licence="No licence stated on the page (ICIMOD publication)",
    claims_supported=[
        "exposed_assets.rishiganga-hep.capacity_mw",
        "exposed_assets.rishiganga-hep.geometry",
        "exposed_assets.rishiganga-hep.status",
        "exposed_assets.tapovan-vishnugad-hep.capacity_mw",
        "exposed_assets.tapovan-vishnugad-hep.status",
    ],
    excerpt=(
        "Table 1: Rishi Ganga Hydropower Project 30.478, 79.699 13.2 MW (Operational); Tapovan "
        "Vishnugad Hydropower Project 30.493, 79.628 520 MW (Under construction). The flood swept "
        "away the unfinished Tapovan Vishnugad HPP and inflicted substantial damage on Rishi Ganga."
    ),
    peer_reviewed=False,
)

CHAMOLI_QUERY = """
[out:json][timeout:180];
(
  way["waterway"]["name"~"Rishi|Ronti|Dhauli",i](30.30,79.45,30.62,79.92);
  way["waterway"]["name:en"~"Rishi|Ronti|Dhauli",i](30.30,79.45,30.62,79.92);
  relation["waterway"~"^(river|stream)$"]["name"~"Rishi|Ronti|Dhauli",i](30.30,79.45,30.62,79.92);
  way["waterway"~"^(river|stream)$"](30.33,79.66,30.44,79.82);
  node["place"]["name"~"Raini|Reni|Tapovan|Joshimath|Jyotirmath|Vishnuprayag|Ronti|Lata|Nanda Devi",i](30.30,79.45,30.62,79.92);
  node["place"]["name:en"~"Raini|Reni|Tapovan|Joshimath|Jyotirmath|Vishnuprayag|Ronti|Lata|Nanda Devi",i](30.30,79.45,30.62,79.92);
  node["natural"="peak"](30.33,79.66,30.44,79.82);
  nwr["power"="plant"](30.30,79.45,30.62,79.92);
  nwr["name"~"Rishiganga|Rishi Ganga|Tapovan|Vishnugad|Hydro",i](30.30,79.45,30.62,79.92);
  nwr["name:en"~"Rishiganga|Rishi Ganga|Tapovan|Vishnugad|Hydro",i](30.30,79.45,30.62,79.92);
);
out geom;
"""

CHAMOLI_RISHIGANGA = AoiSpec(
    id="chamoli-rishiganga",
    name="Ronti Peak - Ronti Gad - Rishiganga - Dhauliganga corridor (Chamoli 2021)",
    countries=("IN",),
    epsg=32644,
    source_zone_bbox=(79.68, 30.33, 79.80, 30.42),
    river_names=("Raunthi Gadhera (Ronti Gad)", "Rishi Ganga", "Dhauli Ganga"),
    overpass_query=CHAMOLI_QUERY,
    downstream_target=(79.575, 30.562),
    chainage_km=40.0,
    fixture_path=f"data/fixtures/osm/chamoli-rishiganga_overpass_{FIXTURE_DATE}.json",
    fixture_retrieved_utc=_utc("2026-09-03T10:22:41Z"),
    transects=(
        TransectSpec(
            "raini",
            "Raini (Reni) village, Rishiganga-Dhauliganga confluence",
            osm_node_id=7833070518,
        ),
        TransectSpec(
            "tapovan",
            "Tapovan (Tapovan Vishnugad barrage site)",
            osm_way_id=683265980,
            notes="No OSM place node for Tapovan; snapped from the plant polygon instead.",
        ),
    ),
    assets=(
        AssetSpec(
            id="rishiganga-hep",
            name="Rishi Ganga Hydropower Project",
            asset_type=AssetType.hydropower_plant,
            status=AssetStatus.damaged,
            source_refs=(ICIMOD_CHAMOLI.id, SHUGAR_2021_ABSTRACT.id),
            stated_location=StatedLocation(
                lon=79.699,
                lat=30.478,
                positional_accuracy_m=500.0,
                basis="coordinates stated in ICIMOD Table 1 (30.478 N, 79.699 E); no OSM feature",
            ),
            capacity_mw=Range(
                low=13.2, high=13.2, best=13.2, unit="MW", source_refs=[ICIMOD_CHAMOLI.id]
            ),
            notes=(
                "Operational before 7 Feb 2021 (ICIMOD); 'substantial damage' (ICIMOD) / "
                "'severely damaging' (Shugar et al.) after the event."
            ),
        ),
        AssetSpec(
            id="tapovan-vishnugad-hep",
            name="Tapovan Vishnugad Hydropower Project",
            asset_type=AssetType.hydropower_plant,
            status=AssetStatus.destroyed,
            source_refs=(ICIMOD_CHAMOLI.id, SHUGAR_2021_ABSTRACT.id),
            osm_way_id=683265980,
            positional_accuracy_m=200.0,
            capacity_mw=Range(
                low=520.0, high=520.0, best=520.0, unit="MW", source_refs=[ICIMOD_CHAMOLI.id]
            ),
            notes=(
                "Under construction before 7 Feb 2021 (ICIMOD); 'swept away' (ICIMOD). Geometry is "
                "the OSM way tagged 'Tapovan Vishnugad Hydropower Plant (Destroyed)'; ICIMOD "
                "states 30.493 N, 79.628 E, 0.3 km from the OSM centroid."
            ),
        ),
    ),
    sources=(SHUGAR_2021, SHUGAR_2021_ABSTRACT, COOK_2021, ICIMOD_CHAMOLI),
    extent_source_refs=(SHUGAR_2021_ABSTRACT.id,),
    notes=(
        "DESIGN CHOICES: the source zone is a rectangle 79.68-79.80 E, 30.33-30.42 N around "
        "Ronti Peak (OSM 'Raunthi' peak 6063 m at 30.369 N, 79.719 E), consistent with the DEM "
        "fixture extent; it is not the mapped detachment scar. The corridor follows the OSM "
        "'Raunthi Gadhera' (Ronti Gad) to the Rishi Ganga, then the Dhauli Ganga to its "
        "confluence with the Alaknanda at Vishnuprayag below Joshimath (downstream target "
        "79.575 E, 30.562 N). Reference: Shugar et al. 2021 and Cook et al. 2021 (DOIs resolved "
        "via Crossref; paywalled, landing pages not retrievable from this host). ASSETS: the two "
        "hydropower projects are sourced to ICIMOD (2021) and the Shugar et al. abstract; the "
        "Vishnuprayag (400 MW, OSM) plant lies below the corridor and was not requested."
    ),
    record_created_utc=RECORD_CREATED,
)

# --- Blatten / Lötschental --------------------------------------------------------------------

VS_2025_05_28 = SourceRef(
    id="vs-ch-2025-05-28-blatten",
    kind=SourceKind.agency_official,
    title="Blatten - Ausserordentliche Mittelzusagen (Staatsrat, Kanton Wallis, 28.05.2025)",
    url="https://www.vs.ch/de/web/communication/detail?groupId=529400&articleId=40208599",
    year=2025,
    publisher="Staatsrat des Kantons Wallis / Conseil d'Etat du Valais",
    accessed_utc=_utc("2026-09-03T10:20:31Z"),
    sha256="a20bc25e4d479646c96c42558279429286a3996e1fb3710f6af94c1046b8ad43",
    content_type="text/html",
    licence="No licence stated (Canton of Valais press release)",
    claims_supported=[
        "exposed_assets.blatten.status",
        "exposed_assets.blatten.population",
        "source_zone.geometry",
    ],
    excerpt=(
        "Ein sehr grosser Teil des Dorfes Blatten wurde zerstört. Das Flussbett der Lonza ist "
        "verstopft. ... Die vollständige Evakuierung des Dorfes Blatten war am Montag, dem 19. "
        "Mai, vorsorglich angeordnet worden. Dreihundert Einwohner wurden evakuiert"
    ),
    peer_reviewed=False,
)

VS_POLIZEIKLAUSEL = SourceRef(
    id="vs-ch-polizeiklausel-loetschental",
    kind=SourceKind.agency_official,
    title=(
        "Allgemeine Polizeiklausel - Strassenzufahrten und provisorische Seilbahn im "
        "Lötschental (Staatsrat, Kanton Wallis)"
    ),
    url=(
        "https://www.vs.ch/de/web/communication/w/clause-g%C3%A9n%C3%A9rale-de-police-acc%C3%A8s-"
        "routiers-et-t%C3%A9l%C3%A9phérique-provisoire-dans-le-l%C3%B6tschental"
    ),
    year=2025,
    publisher="Staatsrat des Kantons Wallis / Conseil d'Etat du Valais",
    accessed_utc=_utc("2026-09-03T10:20:32Z"),
    sha256="2b66ccbae19eed47b8b547ce8402a4cd4fb6c80ac188d2d2cded4ab2f843a6c2",
    content_type="text/html",
    licence="No licence stated (Canton of Valais press release)",
    claims_supported=["exposed_assets.blatten.status", "corridor"],
    excerpt=(
        "... begrub den grössten Teil des Dorfes Blatten sowie die weiter unterhalb gelegenen "
        "Weiler Ried, Oberried und Tännmatten unter sich. Das verheerende Ereignis zerstörte die "
        "Kantonsstrasse NG24 zwischen Wiler und Blatten (einschliesslich Schutzgalerie)"
    ),
    peer_reviewed=False,
)

SWISSINFO_2025_05_19 = SourceRef(
    id="swissinfo-2025-05-19-blatten",
    kind=SourceKind.press_report,
    title="300 Menschen wegen grosser Felssturzgefahr in Blatten evakuiert (Keystone-SDA via SWI)",
    url=(
        "https://www.swissinfo.ch/ger/300-menschen-wegen-grosser-felssturzgefahr-in-blatten-"
        "evakuiert/89344250"
    ),
    year=2025,
    publisher="SWI swissinfo.ch (Keystone-SDA)",
    accessed_utc=_utc("2026-09-03T10:23:06Z"),
    sha256="89ae44e013f35f529b27f49b2bdc4e27b9cc925c70d3e09af8d55c86498fae96",
    content_type="text/html",
    licence="Copyright SWI swissinfo.ch / Keystone-SDA; press report",
    claims_supported=["exposed_assets.blatten.population"],
    excerpt=(
        "Rund 300 Menschen sind am Montag in Blatten VS wegen grosser Felssturzgefahr evakuiert "
        "worden ... Betroffen von der Evakuierung seien 300 Einwohnerinnen und Einwohner und rund "
        "hundert Gebäude, sagte Ebener. (19. Mai 2025)"
    ),
    peer_reviewed=False,
)

SRF_2025_05_18 = SourceRef(
    id="srf-2025-05-18-blatten",
    kind=SourceKind.press_report,
    title="Blatten: konkrete Gefahr durch Bergstürze, 92 Personen evakuiert (SRF, 18.05.2025)",
    url=(
        "https://www.srf.ch/news/schweiz/loetschental-im-wallis-blatten-konkrete-gefahr-durch-"
        "bergstuerze-92-personen-evakuiert"
    ),
    year=2025,
    publisher="Schweizer Radio und Fernsehen (SRF)",
    accessed_utc=_utc("2026-09-03T10:20:35Z"),
    sha256="3636f83c46f4477de191d380b2009c324936714097c6412ed097812c89bb29b1",
    content_type="text/html",
    licence="Copyright SRF; press report",
    claims_supported=["source_zone.geometry", "exposed_assets.blatten.status"],
    excerpt=(
        "... die anhaltende Instabilität im Bereich des Kleinen Nesthorns und des Birchgletscher "
        "an der Südflanke des Lötschentals «eine konkrete Gefahr durch mögliche Bergstürze» "
        "darstelle."
    ),
    peer_reviewed=False,
)

WSL_2025_06_02 = SourceRef(
    id="wsl-2025-06-02-birchgletscher",
    kind=SourceKind.agency_official,
    title="Was Gletscherforschende über den Abbruch des Birchgletschers wissen (ETH Zürich / WSL)",
    url=(
        "https://www.wsl.ch/de/news/was-gletscherforschende-ueber-den-abbruch-des-"
        "birchgletschers-wissen/"
    ),
    year=2025,
    publisher="Eidg. Forschungsanstalt für Wald, Schnee und Landschaft WSL / ETH Zürich",
    accessed_utc=_utc("2026-09-03T10:20:37Z"),
    sha256="9ee7ea77dc99b9fa6d8efc09cb9e7a420cd32ace2d75f0d7c6fdd309ba179fb1",
    content_type="text/html",
    licence="No licence stated (WSL news page)",
    claims_supported=["source_zone.geometry"],
    excerpt=(
        "Am Mittwoch 28. Mai 2025 ist der Birchgletscher unter der Last von Fels- und "
        "Schuttmassen eingebrochen, die von Felsstürzen am Kleinen Nesthorn stammten."
    ),
    peer_reviewed=False,
)

BLATTEN_QUERY = """
[out:json][timeout:180];
(
  way["waterway"]["name"~"Lonza",i](46.28,7.60,46.50,7.95);
  relation["waterway"]["name"~"Lonza",i](46.28,7.60,46.50,7.95);
  node["place"]["name"~"^(Blatten|Wiler|Gampel|Kippel|Ferden|Goppenstein|Steg|Ried|Fafleralp)",i](46.28,7.60,46.50,7.95);
  node["natural"="peak"]["name"~"Nesthorn|Birch",i](46.28,7.60,46.50,7.95);
  nwr["natural"="glacier"]["name"~"Birch",i](46.28,7.60,46.50,7.95);
  nwr["power"="plant"](46.28,7.60,46.50,7.95);
);
out geom;
"""

BLATTEN_LOTSCHENTAL = AoiSpec(
    id="blatten-lotschental",
    name="Kleines Nesthorn - Birchgletscher - Lonza corridor (Blatten 2025)",
    countries=("CH",),
    epsg=32632,
    source_zone_bbox=(7.78, 46.39, 7.87, 46.45),
    river_names=("Lonza",),
    overpass_query=BLATTEN_QUERY,
    downstream_target=(7.735, 46.312),
    chainage_km=30.0,
    fixture_path=f"data/fixtures/osm/blatten-lotschental_overpass_{FIXTURE_DATE}.json",
    fixture_retrieved_utc=_utc("2026-09-03T10:08:48Z"),
    transects=(
        TransectSpec("blatten", "Blatten (Lötschen)", osm_node_id=240114017),
        TransectSpec("wiler", "Wiler (Lötschen)", osm_node_id=240061085),
        TransectSpec("gampel", "Gampel", osm_node_id=60031830),
    ),
    assets=(
        AssetSpec(
            id="blatten",
            name="Blatten (Lötschen)",
            asset_type=AssetType.settlement,
            status=AssetStatus.destroyed,
            source_refs=(VS_2025_05_28.id, VS_POLIZEIKLAUSEL.id, SWISSINFO_2025_05_19.id),
            osm_node_id=240114017,
            positional_accuracy_m=300.0,
            population=Range(
                low=300.0,
                high=300.0,
                best=300.0,
                unit="persons",
                source_refs=[VS_2025_05_28.id, SWISSINFO_2025_05_19.id],
                notes=(
                    "Residents evacuated on 19 May 2025: 300 per the Canton (28.05.2025) and "
                    "Keystone-SDA (300 residents, about 100 buildings); an evacuation count, "
                    "not a census figure."
                ),
            ),
            notes=(
                "Status per the Canton of Valais (28.05.2025): a very large part of the village "
                "was destroyed after the complete evacuation ordered on 19 May 2025."
            ),
        ),
    ),
    sources=(
        VS_2025_05_28,
        VS_POLIZEIKLAUSEL,
        SWISSINFO_2025_05_19,
        SRF_2025_05_18,
        WSL_2025_06_02,
    ),
    extent_source_refs=(VS_2025_05_28.id, WSL_2025_06_02.id, SRF_2025_05_18.id),
    notes=(
        "DESIGN CHOICES: the source zone is a rectangle 7.78-7.87 E, 46.39-46.45 N covering the "
        "Kleines Nesthorn (OSM peak 46.4 N, 7.8 E) and the Birchgletscher above Blatten, as "
        "named by the Canton, WSL/ETH and SRF; it is not the mapped scar or deposit. The "
        "corridor follows the OSM Lonza from Blatten past Wiler to Gampel (downstream target "
        "7.735 E, 46.312 N near the Rhône confluence); chainage cap 30 km (not reached: 16.5 km available). Chainage 0 is the "
        "Lonza node nearest the source-zone centroid, i.e. at Blatten, not the glacier. ASSETS: "
        "Blatten settlement only, as requested; status and evacuation count from the Canton."
    ),
    record_created_utc=RECORD_CREATED,
)

AOI_SPECS: dict[str, AoiSpec] = {
    spec.id: spec for spec in (LHENDE_KHOLA_TRISHULI, CHAMOLI_RISHIGANGA, BLATTEN_LOTSCHENTAL)
}

FIXED_TRANSECT_IDS: dict[str, tuple[str, ...]] = {
    "lhende-khola-trishuli": ("rasuwagadhi-gyirong", "syabrubesi", "betrawati", "galchhi"),
    "chamoli-rishiganga": ("raini", "tapovan"),
    "blatten-lotschental": ("blatten", "wiler", "gampel"),
}
"""Transect ids other components (the event library) reference; they must exist."""
