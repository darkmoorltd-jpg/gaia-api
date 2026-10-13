import os, json
from functools import lru_cache
from app.services import groq_client


SYSTEM = (
    "You are GAIA, Africa's boldest agricultural advisor built by Darkmoor Ltd. "
    "Your job is to save a Nigerian farmer's harvest. Speak like a trusted field officer "
    "who has walked hundreds of farms - direct, specific, no hedging, no 'consult an expert'. "
    "Every answer uses markdown, Naira costs, exact product names sold in Nigerian agro-dealers "
    "(Ridomil, Mancozeb, Emamectin benzoate, Force Up, Dragon, Urea, NPK 20:10:10, etc.), "
    "and gives the farmer a clear do-this-today plan. Never mention AI, Groq, or any tech company."
)

PROMPTS = {
    "crop": """A Nigerian farmer's **{crop}** crop was just diagnosed with **{disease}** at **{confidence:.1f}% confidence**.

Write a bold, high-impact action plan in markdown. Structure:

**One-sentence verdict** - what this is and how serious.

## What This Actually Means
3 sentences. Plain language. No textbook talk.

## DO THIS TODAY (Next 12 Hours)
Numbered list, 5-7 steps. Each step = specific action + exact product + dosage + Naira cost.

## The Cheap Fix (Under 5,000 Naira)
If they only have small money - the one high-ROI action.

## The Full Fix (5,000-50,000 Naira)
Complete spray/fertiliser program with exact products, dosages, timing, cost.

## The Unusual Trick
One non-obvious technique most agronomists never mention. Something clever, cheap, provable. Examples: wood ash slurry, fermented cow urine spray, rice husk mulch, companion planting with marigold or cowpea, plastic bottle drip, salt-water seed soak.

## Water, Spacing, Sanitation
What to change this week.

## Fertilizer Adjustment
Exact NPK ratio. What to stop. What to add. Naira cost.

## Yield Impact
If treated now: X percent saved. If ignored: Y percent lost. Be blunt with numbers.

## Cost Breakdown (Naira)
Markdown table: Item | Qty | Unit Price | Total.

## Prevention for Next Season
Bullet list. Include one seed-variety recommendation (resistant variety name).

## When to Give Up
Be honest. If the field is beyond saving, say so. How to salvage what is left.

Rules:
- Speak TO the farmer (you), not about them.
- Every number in Naira, every product named for Nigeria.
- Short sentences. Bold key terms. No filler.
- Include at least one if you do nothing else, do this line.
- Never mention AI or any tech company.""",

    "pest": """A Nigerian farmer's crop has been infested with **{disease}** at **{confidence:.1f}% confidence**.

Write a bold pest-control action plan in markdown:

**One-sentence verdict.**

## What This Pest Does
3 sentences - damage pattern, speed, what it eats.

## KILL IT TODAY (Next 6 Hours)
Numbered 5-7 steps. Exact insecticide + dosage + Naira cost. Alternate spray timing. Safety gear.

## The Cheap Fix (Under 3,000 Naira)
Neem oil, soap solution, ash, pepper-garlic spray - exact recipe.

## The Chemical Fix
Two products (rotate to prevent resistance). Exact dosage per 15L knapsack. Naira cost. PHI (pre-harvest interval).

## The Unusual Trick
Non-obvious move most farmers never try. Example: planting trap crops (sunflower around the field), releasing ladybird beetles, using sticky yellow cards, pouring wood ash into whorls.

## Field Sanitation
How to remove breeding sites.

## Water & Irrigation
Timing changes that reduce spread.

## Yield at Risk
Blunt numbers - X percent loss if untreated in Y days.

## Cost-Benefit (Naira)
Table: treatment cost vs value of crop saved.

## Prevention Next Season
Rotation, resistant variety, companion planting, biological control.

## When to Give Up
Be honest - when the infestation is unrecoverable.

Rules: Nigerian products, Naira costs, speak to farmer, bold key terms, no filler, never mention AI.""",

    "soil": """A Nigerian farmer's soil was classified as **{disease}** at **{confidence:.1f}% confidence**.

Write a bold soil-management action plan in markdown:

**One-sentence verdict - is this soil good, bad, or fixable?**

## What You Have
3 sentences on texture, drainage, natural fertility.

## Fix It This Week
Numbered steps - organic matter, pH, structure. Specific quantities per acre. Naira cost.

## Best Crops for This Soil
Bullet list with expected yields per acre in Nigeria. Include one if you want cash fast crop.

## Crops That Will Fail
Bullet list - be blunt.

## Organic Boost
Manure, compost, mulch, biochar - exact volumes + Naira cost. Include a homemade compost recipe.

## Fertilizer Program
Exact NPK blend + quantity per acre + timing. What NOT to use.

## The Unusual Trick
Non-obvious technique. Examples: planting legume cover crops (mucuna, cowpea) and tilling in green, using charcoal dust for pH, termite mound soil as fertilizer, mulching with cocoa pod husks.

## pH & Mineral Fix
Lime or gypsum - when, how much, Naira cost.

## Water Management
Ridge, bed, or flat. Irrigation schedule. Drainage fixes for waterlogged areas.

## Yield Potential
Realistic per-acre yield for top 3 crops in this soil.

## Soil Conservation
Stop erosion, keep nutrients.

## Cost Breakdown (Naira)
Table.

Rules: Naira costs, Nigerian crops and products, speak to farmer, bold key terms, no filler, never mention AI.""",

    "livestock": """A Nigerian farmer's **{crop}** was diagnosed with **{disease}** at **{confidence:.1f}% confidence**.

Write a bold livestock emergency plan in markdown:

**One-sentence verdict - is this an emergency?**

## What This Disease Does
3 sentences - severity, contagiousness, transmission.

## ISOLATE & TREAT NOW (Next 6 Hours)
Numbered 5-7 steps. Move the animal. Disinfect. Drug + dosage per kg body weight. Route (oral, injection). Naira cost.

## Isolation Protocol
Distance, duration, who can enter, disinfection products (Virkon S, Izal, bleach dilution).

## Drug Treatment
Specific drug names sold in Nigeria (Oxytetracycline LA, Pen-Strep, Tylosin, Amprolium, Ivermectin). Exact dosage, route, duration, withdrawal period for meat/eggs/milk. Naira cost per animal.

## Supportive Care
Water with electrolytes, high-protein feed, ventilation, warmth, reduce stress.

## The Unusual Trick
Non-obvious move. Examples: adding raw garlic + honey to drinking water, papaya leaf extract for fever, wood ash footbath, clay + charcoal paste on wounds.

## Vaccination & Herd Protection
Vaccinate the healthy animals now. Exact vaccine + schedule + Naira cost.

## Vector Control
Ticks, flies, mosquitoes, rodents - products + schedule.

## Report to Authorities
When to notify the vet or Federal Ministry of Agriculture (notifiable diseases: FMD, Avian Influenza, ASF).

## Economic Impact (Naira)
Animal value at risk, treatment cost, insurance options.

## Prevention Next Season
Biosecurity, quarantine, feed storage, housing.

## When to Cull
Be honest - when to cut losses.

Rules: Nigerian drugs and dosages, Naira costs, speak to farmer, bold key terms, no filler, never mention AI.""",
}


@lru_cache(maxsize=200)
def _generate(model_type, crop, disease, confidence_rounded):
    template = PROMPTS.get(model_type, PROMPTS["crop"])
    prompt = template.format(crop=crop or "crop", disease=disease, confidence=confidence_rounded)
    try:
        return groq_client.chat_text(prompt, system_override=SYSTEM)
    except Exception as e:
        return "**Recommendation unavailable.** " + str(e)


def get_recommendations(model_key, top_label, confidence, extra_context=""):
    model_type = "crop"
    if model_key.startswith("pests"):
        model_type = "pest"
    elif model_key.startswith("soil"):
        model_type = "soil"
    elif model_key in ("cattle", "poultry"):
        model_type = "livestock"

    confidence_rounded = round(float(confidence), 1)
    return _generate(model_type, extra_context, top_label, confidence_rounded)
