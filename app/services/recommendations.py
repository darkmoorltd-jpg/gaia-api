import os
import json
from functools import lru_cache

from app.services import groq_client


PROMPTS = {
    "crop": """You are GAIA, Africa's leading crop disease advisor.

The farmer's {crop} crop has been diagnosed with: **{disease}** ({confidence:.1f}% confidence).

Produce a structured action plan in markdown with these exact sections:

## What This Means
2-3 sentences explaining the disease in plain language.

## Immediate Action (Next 24 Hours)
Numbered steps the farmer must do TODAY.

## Organic Treatment
Specific products, dosages per litre or per acre, frequency, Naira cost.

## Chemical Treatment
Specific product names (Mancozeb, Ridomil, etc.), dosage, safety precautions, Naira cost.

## Fertilizer Adjustment
What fertilizer to apply now (NPK ratios, organic manure), and what to avoid.

## Water & Field Management
Irrigation changes, spacing, ridge/bed preparation, sanitation.

## Yield Impact & Timeline
Expected yield loss if untreated, recovery time, next harvest outlook.

## Prevention (Next Season)
Bullet list of long-term practices.

## Estimated Cost
Table of costs in Naira: chemicals, fertilizer, labour, total.

Be specific. Use African/Nigerian product names and Naira prices. Never mention AI or Groq.""",

    "pest": """You are GAIA, Africa's leading pest management advisor.

The farmer's crop is infested with: **{disease}** ({confidence:.1f}% confidence).

Produce a structured action plan in markdown with these exact sections:

## About This Pest
2-3 sentences on the pest, its lifecycle, and damage pattern.

## Immediate Action (Next 24 Hours)
Numbered steps to contain the outbreak.

## Organic Control
Neem oil, soap spray, biological agents — with dosages and Naira costs.

## Chemical Control
Specific insecticides, dosage per litre, safety, Naira cost.

## Herbicide / Field Care
Weeding and sanitation to remove breeding sites.

## Water & Irrigation
Irrigation adjustments to reduce pest spread.

## Yield Protection
How much yield is at risk and what to protect first.

## Prevention
Crop rotation, companion planting, resistant varieties.

## Cost-Benefit
Table with Naira costs vs Naira yield saved.

Use African/Nigerian products. Never mention AI or Groq.""",

    "soil": """You are GAIA, Africa's leading soil scientist.

The farmer's soil has been classified as: **{disease}** ({confidence:.1f}% confidence).

Produce a structured action plan in markdown with these exact sections:

## Soil Characteristics
2-3 sentences on this soil type's texture, drainage, and natural fertility.

## Best Crops to Plant
Bullet list of 5-8 crops that thrive in this soil.

## Crops to Avoid
Bullet list of crops that will struggle.

## Organic Improvement
Manure, compost, mulch — quantities per acre and Naira costs.

## Fertilizer Recommendation
Exact NPK blend, quantity per acre, timing, Naira cost.

## pH & Mineral Adjustment
Lime or gypsum if needed, quantity and cost.

## Water Management
Irrigation frequency, drainage improvements.

## Land Preparation
Ridge, bed, or flat planting — best method for this soil.

## Yield Potential
Expected yield range for top crops in this soil.

## Soil Conservation
How to prevent erosion and nutrient loss.

Be specific. Nigerian Naira prices. Never mention AI or Groq.""",

    "livestock": """You are GAIA, Africa's leading livestock veterinarian.

The animal has been diagnosed with: **{disease}** ({confidence:.1f}% confidence).

Produce a structured action plan in markdown with these exact sections:

## What This Means
2-3 sentences on the disease, its severity, and contagiousness.

## Immediate Action (Next 12 Hours)
Numbered steps — isolate, disinfect, notify, observe.

## Isolation Protocol
How far, how long, who can enter, disinfection.

## Treatment (Drugs)
Specific drug names (Oxytetracycline, Pen-Strep, etc.), dosage per kg body weight, route of administration, duration, Naira cost.

## Supportive Care
Water, feed, electrolytes, ventilation, temperature.

## Vaccination
Next vaccine due, schedule for other animals, booster timing.

## Vector Control
Flies, ticks, mosquitoes — products and frequency.

## Reporting to Authorities
When to notify the vet or ministry of agriculture.

## Prevention
Biosecurity practices, quarantine for new animals.

## Economic Impact
Naira value of animal at risk, cost of treatment, insurance options.

Use Nigerian products and Naira. Never mention AI or Groq.""",
}


@lru_cache(maxsize=200)
def _generate(model_type, crop, disease, confidence_rounded):
    template = PROMPTS.get(model_type, PROMPTS["crop"])
    prompt = template.format(crop=crop or "crop", disease=disease, confidence=confidence_rounded)
    try:
        return groq_client.chat_text(prompt)
    except Exception as e:
        return "**Recommendation unavailable.** " + str(e)


def get_recommendations(model_key, top_label, confidence, extra_context=""):
    """
    Returns markdown recommendations for the detected condition.
    Cached so repeat detections are instant.
    """
    # Map model key to type
    model_type = "crop"
    if model_key.startswith("pests"):
        model_type = "pest"
    elif model_key.startswith("soil"):
        model_type = "soil"
    elif model_key in ("cattle", "poultry"):
        model_type = "livestock"

    confidence_rounded = round(confidence, 1)
    return _generate(model_type, extra_context, top_label, confidence_rounded)
