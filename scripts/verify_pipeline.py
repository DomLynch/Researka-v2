#!/usr/bin/env python3
"""Submit a paper to Researka v2 and run the full pipeline."""
import json
import httpx
import time

BASE = "http://49.12.7.18:8000"

paper = {
    "title": "Rapid Evidence Synthesis: Can 2025-2026 mRNA vaccine platforms deliver durable protection against emerging infectious diseases beyond COVID-19?",
    "abstract": "Test submission for live pipeline verification.",
    "domain_slug": "infectious_disease",
    "author_agent_id": "audit-v2",
    "sections": {
        "Research Question": "This submission examines whether 2025-2026 evidence demonstrates that mRNA vaccine platforms can deliver durable protective immunity against emerging infectious diseases beyond COVID-19, considering both the breadth of pathogen targets under active investigation in clinical settings, the duration and quality of immune responses observed across completed and ongoing trials, the scalability and speed of manufacturing processes required for rapid deployment, and whether current preliminary results justify measured optimism for robust pandemic response capabilities in future outbreaks across diverse populations worldwide.",
        "Search Summary": "PubMed and ClinicalTrials.gov searched for 2025-2026 mRNA vaccine trials targeting non-COVID pathogens including influenza, RSV, CMV, and emerging viral threats.",
        "Evidence Landscape": "The bundle covers mRNA platforms targeting multiple pathogen classes with 12 sources spanning reviews and primary clinical data from 2024-2025.",
        "Key Findings": "mRNA platforms demonstrate strong immunogenicity across multiple pathogen targets with rapid development timelines and acceptable safety profiles in early-phase trials.",
        "Limitations": "Most data is from early-phase trials with limited durability follow-up beyond 12 months. Manufacturing scalability for non-COVID targets remains unproven at commercial scale.",
        "Gaps Identified": "No large-scale Phase 3 efficacy data exists for most non-COVID mRNA vaccine candidates. Duration of protection beyond 12 months is uncharacterized.",
        "Conclusion": "mRNA vaccine platforms show broad potential across multiple infectious disease targets but durability data and real-world efficacy evidence remain limited and early-stage.",
    },
    "source_bundle": [
        {"title": "mRNA influenza vaccine Phase 2 results", "year": 2025, "evidence_type": "primary"},
        {"title": "RSV mRNA vaccine immunogenicity data", "year": 2025, "evidence_type": "primary"},
        {"title": "CMV mRNA vaccine Phase 1 safety", "year": 2025, "evidence_type": "primary"},
        {"title": "mRNA platform scalability review", "year": 2025, "evidence_type": "review"},
        {"title": "Emerging viral threats mRNA pipeline", "year": 2025, "evidence_type": "review"},
        {"title": "Immune durability in mRNA vaccines", "year": 2024, "evidence_type": "primary"},
        {"title": "mRNA manufacturing process optimization", "year": 2025, "evidence_type": "review"},
        {"title": "Pandemic preparedness mRNA platforms", "year": 2025, "evidence_type": "review"},
        {"title": "mRNA vaccine safety long-term monitoring", "year": 2024, "evidence_type": "review"},
        {"title": "Cross-reactive immunity from mRNA vaccines", "year": 2024, "evidence_type": "primary"},
        {"title": "mRNA lipid nanoparticle delivery advances", "year": 2025, "evidence_type": "primary"},
        {"title": "mRNA vaccine regulatory pathways", "year": 2024, "evidence_type": "review"},
    ],
}

print("=== Submitting ===")
r = httpx.post(f"{BASE}/submissions", json=paper, timeout=30)
data = r.json()
submission_id = data["submission"]["id"]
print(f"ID: {submission_id}")

print("\n=== Running pipeline ===")
for i in range(1, 8):
    r = httpx.post(f"{BASE}/jobs/run-once", timeout=60)
    result = r.json()
    print(f"run {i}: claimed={result['claimed']} completed={result['completed']} failed={result['failed']}")
    if result["claimed"] == 0:
        break

print("\n=== Decision ===")
r = httpx.get(f"{BASE}/submissions/{submission_id}/decision", timeout=10)
print(json.dumps(r.json(), indent=2))

print("\n=== Publications ===")
r = httpx.get(f"{BASE}/publications", timeout=10)
pubs = r.json()["publications"]
print(f"Count: {len(pubs)}")
for p in pubs:
    print(f"  - {p['title']}")
    print(f"    metadata: provider={p['metadata'].get('provider')} route={p['metadata'].get('route', 'N/A')}")
