import re
from typing import Dict, List, Tuple


def ai_categorize_medical_file(filename: str, extracted_text: str = "") -> Tuple[str, float, Dict[str, any]]:
    """
    Enhanced AI-powered medical file categorization using both filename and extracted content.
    Returns category, confidence score, and metadata.
    """
    # Normalize inputs
    name = filename.lower().replace("-", " ").replace("_", " ").replace(".", " ")
    text = extracted_text.lower() if extracted_text else ""
    
    # Enhanced categorization rules with content patterns
    categories = {
        "Prescription": {
            "filename_keywords": ["prescription", "rx", "medicine", "medication", "drug", "pharmacy"],
            "content_patterns": [
                r"rx\s*#?", r"prescription\s*#", r"take\s+\d+\s+(tablet|capsule|pill)",
                r"mg\s+(daily|bid|tid|qid)", r"refills?\s*\d+", r"pharmacy",
                r"dispense\s+\d+", r"sig\s*:", r"directions?"
            ],
            "medical_terms": ["dosage", "frequency", "refill", "dispense", "pharmacy"],
            "weight": 1.0
        },
        "Blood Report": {
            "filename_keywords": ["blood", "cbc", "hemoglobin", "hb", "platelet", "rbc", "wbc"],
            "content_patterns": [
                r"complete\s+blood\s+count", r"cbc\s*:", r"hemoglobin\s*:",
                r"platelet\s+count", r"rbc\s+count", r"wbc\s+count",
                r"neutrophils?", r"lymphocytes?", r"monocytes?",
                r"g/dl", r"cells?/mm3", r"×10⁹/l"
            ],
            "medical_terms": ["hemoglobin", "platelet", "rbc", "wbc", "neutrophils", "lymphocytes"],
            "weight": 1.0
        },
        "Lab Report": {
            "filename_keywords": ["lab", "test", "pathology", "biochemistry", "report"],
            "content_patterns": [
                r"laboratory\s+report", r"pathology\s+report", r"test\s+result",
                r"reference\s+range", r"normal\s+range", r"abnormal",
                r"specimen\s*:", r"collected\s*:", r"analyzed\s*:",
                r"mg/dl", r"mmol/l", r"iu/l", r"pg/ml"
            ],
            "medical_terms": ["specimen", "reference", "range", "abnormal", "analyzed"],
            "weight": 0.9
        },
        "Scan/Imaging": {
            "filename_keywords": ["xray", "x-ray", "ct", "mri", "scan", "ultrasound", "usg", "radiology"],
            "content_patterns": [
                r"chest\s+x-ray", r"ct\s+scan", r"mri\s+scan", r"ultrasound",
                r"radiology\s+report", r"imaging\s+study", r"contrast\s+",
                r"findings\s*:", r"impression\s*:", r"radiograph",
                r"sonography", r"echocardiogram", r"doppler"
            ],
            "medical_terms": ["radiology", "contrast", "findings", "impression", "radiograph"],
            "weight": 1.0
        },
        "Discharge Summary": {
            "filename_keywords": ["discharge", "hospital", "summary", "admission"],
            "content_patterns": [
                r"discharge\s+summary", r"hospital\s+course", r"admission\s+date",
                r"discharge\s+date", r"final\s+diagnosis", r"discharge\s+instructions",
                r"hospital\s+:", r"patient\s+was\s+admitted", r"discharged\s+on"
            ],
            "medical_terms": ["admission", "discharge", "diagnosis", "hospital", "course"],
            "weight": 1.0
        },
        "Insurance": {
            "filename_keywords": ["insurance", "claim", "coverage", "policy", "eob"],
            "content_patterns": [
                r"insurance\s+claim", r"explanation\s+of\s+benefits", r"policy\s+#",
                r"claim\s+#", r"coverage\s+determination", r"preauthorization",
                r"deductible", r"copay", r"coinsurance"
            ],
            "medical_terms": ["claim", "coverage", "policy", "deductible", "copay"],
            "weight": 0.8
        },
        "Vaccination": {
            "filename_keywords": ["vaccine", "vaccination", "immunization", "covid"],
            "content_patterns": [
                r"vaccination\s+record", r"immunization\s+history", r"covid-?\d+\s+vaccine",
                r"dose\s+\d+", r"booster", r"vaccine\s+type",
                r"administration\s+date", r"next\s+due"
            ],
            "medical_terms": ["vaccine", "immunization", "dose", "booster", "administration"],
            "weight": 0.9
        },
        "Doctor's Note": {
            "filename_keywords": ["note", "consultation", "visit", "followup", "progress"],
            "content_patterns": [
                r"doctor'?s?\s+note", r"consultation\s+note", r"progress\s+note",
                r"follow\s+up\s+visit", r"subjective\s*:", r"objective\s*:",
                r"assessment\s*:", r"plan\s*:", r"soap\s+note"
            ],
            "medical_terms": ["consultation", "followup", "subjective", "objective", "assessment"],
            "weight": 0.8
        }
    }
    
    best_category = "Uncategorized"
    best_score = 0.0
    metadata = {
        "filename_matches": [],
        "content_matches": [],
        "medical_term_matches": [],
        "extracted_dates": [],
        "confidence_factors": []
    }
    
    # Score each category
    for category, rules in categories.items():
        score = 0.0
        category_matches = {
            "filename": [],
            "content": [],
            "medical_terms": []
        }
        
        # Filename keyword matching
        filename_score = 0
        for keyword in rules["filename_keywords"]:
            if keyword in name:
                filename_score += 1
                category_matches["filename"].append(keyword)
        
        # Content pattern matching
        content_score = 0
        if text:
            for pattern in rules["content_patterns"]:
                matches = re.findall(pattern, text, re.IGNORECASE)
                if matches:
                    content_score += len(matches) * 2  # Weight content matches higher
                    category_matches["content"].extend(matches[:3])  # Limit to first 3 matches
        
        # Medical term matching
        medical_score = 0
        if text:
            for term in rules["medical_terms"]:
                if term in text:
                    medical_score += 1
                    category_matches["medical_terms"].append(term)
        
        # Calculate weighted score
        total_score = (filename_score * 0.3 + content_score * 0.5 + medical_score * 0.2) * rules["weight"]
        
        if total_score > best_score:
            best_score = total_score
            best_category = category
            metadata["filename_matches"] = category_matches["filename"]
            metadata["content_matches"] = category_matches["content"]
            metadata["medical_term_matches"] = category_matches["medical_terms"]
    
    # Extract dates from document
    if text:
        date_patterns = [
            r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",  # MM/DD/YYYY or MM-DD-YYYY
            r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b",    # YYYY/MM/DD or YYYY-MM-DD
            r"\b\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}\b"  # Month DD, YYYY
        ]
        for pattern in date_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            metadata["extracted_dates"].extend(matches[:5])  # Limit to first 5 dates
    
    # Calculate confidence
    if best_score == 0:
        confidence = 0.1
        metadata["confidence_factors"] = ["No matching patterns found"]
    else:
        confidence = min(0.95, best_score / 3.0)  # Normalize to 0-0.95 range
        metadata["confidence_factors"] = [
            f"Filename matches: {len(metadata['filename_matches'])}",
            f"Content patterns: {len(metadata['content_matches'])}",
            f"Medical terms: {len(metadata['medical_term_matches'])}"
        ]
    
    return best_category, round(confidence, 2), metadata


def categorize_medical_file(filename: str) -> tuple[str, float]:
    """
    Enhanced categorization using filename and extracted content.
    Falls back to filename-only if no content is available.
    """
    name = filename.lower().replace("-", " ").replace("_", " ")
    rules = {
        "Prescription": ["prescription", "rx", "medicine", "medication"],
        "Blood Report": ["blood", "cbc", "hemoglobin", "hb", "platelet"],
        "Lab Report": ["lab", "test", "pathology", "biochemistry"],
        "Scan/Imaging": ["xray", "x-ray", "ct", "mri", "scan", "ultrasound", "usg"],
        "Discharge Summary": ["discharge", "hospital", "summary"],
        "Insurance": ["insurance", "claim", "coverage", "policy"],
        "Vaccination": ["vaccine", "vaccination", "immunization", "covid"],
        "Doctor's Note": ["note", "consultation", "visit", "followup", "progress"]
    }
    best_category = "Uncategorized"
    best_score = 0
    for category, keywords in rules.items():
        score = sum(1 for kw in keywords if kw in name)
        if score > best_score:
            best_score = score
            best_category = category
    if best_score == 0:
        return ("Uncategorized", 0.25)
    return (best_category, round(min(0.55 + best_score * 0.18, 0.98), 2))

