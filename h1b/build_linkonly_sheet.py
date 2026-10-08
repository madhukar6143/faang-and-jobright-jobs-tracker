"""One-off: spreadsheet of H-1B sponsors the tracker CANNOT auto-fetch.

These run closed/custom career sites (like Walmart) with no public ATS feed,
so they're apply-manually. Native-connector companies (Amazon/Apple/Microsoft/
Google) are excluded -- those ARE fetched.
"""
import json
from pathlib import Path
from urllib.parse import quote_plus

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = Path(__file__).parent
NATIVE = {"Amazon", "Apple", "Microsoft", "Google"}   # actually fetched

# Curated direct careers pages for the big link-only names (tech-filtered where
# the site supports it). Anything not here falls back to a LinkedIn search.
CAREERS = {
    "Meta": "https://www.metacareers.com/jobs",
    "Intel": "https://jobs.intel.com/en/search-jobs/software",
    "Qualcomm": "https://careers.qualcomm.com/careers?query=software%20engineer",
    "NVIDIA": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    "Cisco": "https://jobs.cisco.com/jobs/SearchJobs/software%20engineer",
    "Oracle": "https://careers.oracle.com/jobs/",
    "IBM": "https://www.ibm.com/careers/search?field_keyword_08[0]=Software%20Engineering",
    "SAP": "https://jobs.sap.com/search/?q=software+engineer&locationsearch=United+States",
    "VMware": "https://careers.vmware.com/main/jobs?keywords=software%20engineer",
    "Dell Technologies": "https://jobs.dell.com/search-jobs/software%20engineer",
    "Hewlett Packard Enterprise": "https://careers.hpe.com/us/en/search-results?keywords=software%20engineer",
    "Tesla": "https://www.tesla.com/careers/search/?query=software%20engineer",
    "Netflix": "https://explore.jobs.netflix.net/careers?query=software%20engineer",
    "Walmart": "https://careers.walmart.com/technology",
    "Infosys": "https://career.infosys.com/jobsearch",
    "Wipro": "https://careers.wipro.com/careers-home/jobs?keywords=software",
    "Cognizant": "https://careers.cognizant.com/global-en/jobs/?keyword=software%20engineer",
    "HCLTech": "https://www.hcltech.com/careers",
    "Tech Mahindra": "https://careers.techmahindra.com/",
    "LTIMindtree": "https://www.ltimindtree.com/careers/",
    "Capgemini": "https://www.capgemini.com/jobs/",
    "Accenture": "https://www.accenture.com/us-en/careers/jobsearch?jk=software+engineer",
    "Genpact": "https://www.genpact.com/careers",
    "Mphasis": "https://careers.mphasis.com/",
    "EPAM Systems": "https://www.epam.com/careers/job-listings",
    "Cyient": "https://www.cyient.com/careers",
    "Persistent Systems": "https://careers.persistent.com/",
    "Coforge": "https://careers.coforge.com/",
    "Virtusa": "https://careers.virtusa.com/",
    "UST": "https://www.ust.com/en/careers",
    "Hexaware Technologies": "https://hexaware.com/careers/",
    "Globant": "https://careers.globant.com/",
    "Birlasoft": "https://www.birlasoft.com/company/careers",
    "Zensar Technologies": "https://www.zensar.com/careers",
    "Deloitte": "https://apply.deloitte.com/en_US/careers/SearchJobs/software%20engineer",
    "EY": "https://careers.ey.com/ey/search/?q=software+engineer",
    "PwC": "https://jobs.us.pwc.com/search-jobs/software%20engineer",
    "KPMG": "https://www.kpmguscareers.com/jobsearch/",
    "Slalom": "https://www.slalom.com/us/en/careers/find-a-career",
    "JPMorgan Chase": "https://careers.jpmorgan.com/us/en/search-results?keywords=software%20engineer",
    "Goldman Sachs": "https://www.goldmansachs.com/careers/our-firm/engineering/",
    "Bank of America": "https://careers.bankofamerica.com/en-us/job-search?search=software+engineer",
    "Wells Fargo": "https://www.wellsfargojobs.com/en/jobs/?search=software+engineer",
    "Citi": "https://jobs.citi.com/search-jobs/software%20engineer",
    "American Express": "https://aexp.eightfold.ai/careers?query=software%20engineer",
    "Charles Schwab": "https://www.schwabjobs.com/search-jobs/software%20engineer",
    "Fidelity Investments": "https://jobs.fidelity.com/search-jobs/software%20engineer",
    "Bloomberg": "https://careers.bloomberg.com/job/search?q=software+engineer",
    "Nasdaq": "https://www.nasdaq.com/about/careers",
    "Morgan Stanley": "https://www.morganstanley.com/careers/career-opportunities-search",
    "Capital One": "https://www.capitalonecareers.com/search-jobs/software%20engineer",
    "Discover Financial": "https://jobs.discover.com/search-jobs/software%20engineer",
    "Synchrony": "https://www.synchronycareers.com/search-jobs/software%20engineer",
    "American Airlines": "https://jobs.aa.com/search-jobs/software%20engineer",
    "U.S. Bank": "https://careers.usbank.com/global/en/search-results?keywords=software%20engineer",
    "Ally Financial": "https://www.ally.com/careers/",
    "Citadel": "https://www.citadel.com/careers/open-opportunities/",
    "Two Sigma": "https://careers.twosigma.com/careers/SearchJobs/",
    "Hudson River Trading": "https://www.hudsonrivertrading.com/careers/",
    "D. E. Shaw": "https://www.deshaw.com/careers",
    "DRW": "https://drw.com/careers/opportunities",
    "Optiver": "https://optiver.com/working-at-optiver/career-opportunities/",
    "Millennium": "https://www.mlp.com/careers/",
    "SIG": "https://careers.sig.com/",
    "Bridgewater Associates": "https://www.bridgewater.com/working-at-bridgewater",
    "Balyasny Asset Management": "https://www.bam.com/careers/",
    "Peak6": "https://peak6.com/careers/",
    "XTX Markets": "https://www.xtxmarkets.com/careers/",
    "Quantlab": "https://www.quantlab.com/careers",
    "Five Rings": "https://fiverings.com/",
    "Wolverine Trading": "https://www.wolve.com/careers",
    "Headlands Technologies": "https://www.headlandstech.com/careers/",
    "Shopify": "https://www.shopify.com/careers/search?keywords=engineer",
    "Atlassian": "https://www.atlassian.com/company/careers/all-jobs?team=Engineering",
    "Snap": "https://careers.snap.com/jobs?role=Engineering",
    "Grammarly": "https://www.grammarly.com/careers/jobs",
    "Rippling": "https://www.rippling.com/careers/open-roles",
    "Robinhood Markets": "https://careers.robinhood.com/",
    "Wiz": "https://www.wiz.io/careers",
    "HashiCorp": "https://www.hashicorp.com/careers/open-positions",
    "Hugging Face": "https://apply.workable.com/huggingface/",
    "Groq": "https://groq.com/careers/",
    "Yahoo": "https://www.yahooinc.com/careers/",
    "Slack": "https://slack.com/careers",
    "Box": "https://www.box.com/careers",
    "Snyk": "https://snyk.io/careers/",
    "Tenable": "https://www.tenable.com/careers",
    "Rapid7": "https://www.rapid7.com/about/careers/",
    "SentinelOne": "https://www.sentinelone.com/careers/",
    "Unity": "https://careers.unity.com/",
    "Electronic Arts": "https://www.ea.com/careers",
    "HubSpot": "https://www.hubspot.com/careers/jobs",
    "Zendesk": "https://www.zendesk.com/jobs/",
    "Yelp": "https://www.yelp.careers/us/en",
    "Redfin": "https://www.redfin.com/careers/positions",
    "AMD": "https://careers.amd.com/careers-home/jobs?keywords=software",
    "Texas Instruments": "https://careers.ti.com/search-jobs/software/",
    "Synopsys": "https://careers.synopsys.com/",
    "Micron Technology": "https://careers.micron.com/careers",
    "Arm": "https://careers.arm.com/search-jobs",
    "ASML": "https://www.asml.com/en/careers/find-your-job",
    "Marvell Technology": "https://www.marvell.com/company/careers.html",
    "Lam Research": "https://careers.lamresearch.com/",
    "Keysight Technologies": "https://careers.keysight.com/",
    "Teradyne": "https://careers.teradyne.com/",
    "Qorvo": "https://careers.qorvo.com/",
    "Skyworks Solutions": "https://careers.skyworksinc.com/",
    "onsemi": "https://www.onsemi.com/careers",
    "Ambarella": "https://careers.ambarella.com/",
    "SiFive": "https://www.sifive.com/careers",
    "Lattice Semiconductor": "https://www.latticesemi.com/en/About/Careers",
    "Blue Origin": "https://www.blueorigin.com/careers",
    "Rivian": "https://careers.rivian.com/",
    "Aurora Innovation": "https://aurora.tech/careers",
    "Cruise": "https://www.getcruise.com/careers/jobs/",
    "Joby Aviation": "https://www.jobyaviation.com/careers/",
    "Relativity Space": "https://www.relativityspace.com/careers",
    "Firefly Aerospace": "https://firefly.com/careers/",
    "Sierra Space": "https://sierraspace.com/careers/",
    "Axiom Space": "https://www.axiomspace.com/careers",
    "Applied Intuition": "https://www.appliedintuition.com/careers",
    "Eli Lilly": "https://careers.lilly.com/us/en/search-results?keywords=software",
    "Moderna": "https://www.modernatx.com/careers/open-roles",
    "Illumina": "https://www.illumina.com/company/careers.html",
    "Tempus AI": "https://www.tempus.com/about-us/careers/",
    "Oscar Health": "https://www.hioscar.com/careers",
    "Guardant Health": "https://www.guardanthealth.com/careers/",
    "Deel": "https://www.deel.com/careers/",
    "Remitly": "https://www.remitly.com/us/en/careers",
    "DigitalOcean": "https://www.digitalocean.com/careers",
    "Sonos": "https://www.sonos.com/en-us/careers",
    "Warby Parker": "https://www.warbyparker.com/careers",
    "Nike": "https://careers.nike.com/",
    "Starbucks": "https://careers.starbucks.com/",
    "Costco": "https://www.costco.com/careers.html",
    "Verizon": "https://mycareer.verizon.com/",
    "AT&T": "https://www.att.jobs/search-jobs/software%20engineer",
}


def link_for(name):
    if name in CAREERS:
        return CAREERS[name], "direct careers page"
    return (f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(name + ' software engineer')}"
            f"&location=United%20States&f_E=2%2C3", "LinkedIn search")


def main():
    boards = json.loads((HERE / "boards.json").read_text(encoding="utf-8"))
    linkonly = [b for b in boards if not b.get("ats") and b["name"] not in NATIVE]
    tier_rank = {"Top 25 sponsor": 0, "Large sponsor": 1, "Active sponsor": 2}
    linkonly.sort(key=lambda b: (tier_rank.get(b["tier"], 9), b["name"].lower()))

    wb = Workbook()
    ws = wb.active
    ws.title = "Apply manually"
    headers = ["Company", "H-1B Sponsor Tier", "Industry", "Link Type", "Careers / Search Link"]
    ws.append(headers)
    hdr_fill = PatternFill("solid", fgColor="1F4E79")
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = hdr_fill
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 22

    link_font = Font(color="0563C1", underline="single")
    for b in linkonly:
        url, kind = link_for(b["name"])
        ws.append([b["name"], b["tier"], b["industry"], kind, "Open careers page"])
        cell = ws.cell(row=ws.max_row, column=5)
        cell.hyperlink = url
        cell.font = link_font

    for i, w in enumerate([30, 17, 24, 20, 26], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:E{ws.max_row}"

    direct = sum(1 for b in linkonly if b["name"] in CAREERS)
    out = HERE / "h1b_sponsors_manual_apply.xlsx"
    wb.save(out)
    print(f"{len(linkonly)} link-only sponsors | {direct} direct careers links, "
          f"{len(linkonly)-direct} via LinkedIn search")
    print(f"Saved -> {out}")


if __name__ == "__main__":
    main()
