"""Retrieval vocabulary: life-domain keywords and sensitive-context cues.

One definition. These tables were copied verbatim into retrieval.py,
retrieval_layers.py and retrieval_graph.py, so an edit to one silently
diverged the lanes.
"""
from __future__ import annotations

DOMAIN_KEYWORDS: dict[str, set[str]] = {
    "physical": {
        "body", "sleep", "health", "fitness", "exercise", "pain", "energy",
        "doctor", "appointment", "medication", "therapy", "diagnosis", "symptom",
        "clinic", "hospital", "physical", "weight", "diet", "eating", "tired",
        "sick", "injury", "recovery", "surgery", "prescription", "checkup",
        "dr.", "dr ", "physician", "therapist",
    },
    "environmental": {
        "home", "room", "space", "environment", "apartment", "house",
        "rent", "lease", "move", "moving", "neighborhood", "city", "place",
        "office", "commute", "landlord", "furniture", "clean", "declutter",
    },
    "financial": {
        "money", "budget", "cash", "expense", "income", "tax", "finance",
        "salary", "savings", "spending", "pay", "bill", "debt", "loan",
        "invest", "investment", "afford", "cost", "price", "payment", "raise",
        "bonus", "account", "bank", "mortgage", "insurance",
    },
    "relational": {
        "friend", "partner", "family", "relationship", "dating", "marriage",
        "son", "daughter", "brother", "sister", "mom", "dad", "mother", "father",
        "wife", "husband", "boyfriend", "girlfriend", "fiance", "colleague",
        "coworker", "neighbor", "cousin", "uncle", "aunt", "grandma", "grandpa",
        "breakup", "divorce", "engaged", "wedding", "baby", "pregnant",
    },
    "work": {
        "work", "job", "project", "team", "meeting", "deadline", "client",
        "boss", "manager", "promotion", "fired", "hired", "resign", "quit",
        "interview", "offer", "onboard", "sprint", "launch", "product",
        "colleague", "performance", "review", "feedback", "career",
    },
    "status": {"status", "reputation", "credibility", "resume", "linkedin", "profile"},
    "appearance": {"appearance", "look", "style", "dress", "hair", "clothes", "outfit", "presentation"},
    "competence": {"competence", "skill", "capable", "ability", "performance", "learn", "practice", "improve"},
    "social_presence": {"social", "presence", "network", "community", "visibility", "post", "audience", "followers"},
    "desirability": {"desirable", "attractive", "desired", "romantic", "dating", "attractive"},
}

SENSITIVE_COMPARTMENTS = {
    "legal": {"legal", "lawsuit", "contract", "attorney", "court"},
    "health": {"health", "medical", "doctor", "diagnosis", "therapy"},
    "children": {"child", "children", "kid", "custody", "school"},
}
