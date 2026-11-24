# The shopping assistant's question set for the load generator, grouped by use case (new file, not in upstream).
# Product names and prices come from the demo's product catalogue (src/product-catalog/products/products.json).
# "buy" questions embed one of the demo's fake customers (people.json) so the agent can call checkout; each success is a
# real order in the demo (demo.order.amount on the checkout span), which is what the revenue beat charts.
# Also the golden set for offline eval: `expect` = substrings a correct answer should contain.
import json
import random

USE_CASE_WEIGHTS = {"product_search": 6, "promotions": 3, "shipping_currency": 3, "buy": 2}

QUESTIONS = {
    "product_search": [
        {"q": "Show all available products in the store.", "expect": ["Solar System Color Imager", "Roof Binoculars"]},
        {"q": "Do you sell telescopes? Which ones and how much do they cost?", "expect": ["Starsense Explorer Refractor Telescope", "349.95"]},
        {"q": "Tell me about the Solar System Color Imager.", "expect": ["175"]},
        {"q": "What's the cheapest product you have?", "expect": ["Comet Book"]},
        {"q": "I'm a beginner. Which telescope would you recommend, and what accessories would I need with it?",
         "expect": ["Telescope"]},
    ],
    "promotions": [
        {"q": "What current promotions are available on binoculars?", "expect": []},
        {"q": "Any deals on telescopes this week?", "expect": []},
        {"q": "What goes well with the Roof Binoculars?", "expect": []},
    ],
    "shipping_currency": [
        {"q": "What currencies are supported by the Astronomy Shop?", "expect": ["USD", "EUR"]},
        {"q": "Can I pay in Australian dollars?", "expect": ["AUD"]},
        {"q": "How much is shipping for two Eclipsmart Travel Refractor Telescopes to 1 Macquarie Street, Sydney, NSW 2000, Australia?",
         "expect": []},
    ],
    "buy": [
        {"q": "Add one Solar System Color Imager to my cart and check out for me. My details: {customer}",
         "expect": ["order"], "products": ["0PUK6V6EV0"]},
        {"q": "Please buy a Lens Cleaning Kit and a Starsense Explorer Refractor Telescope for me and complete the checkout. My details: {customer}",
         "expect": ["order"], "products": ["L9ECAV7KIM", "66VCHSJNUP"]},
    ],
}


def _customer_text(person, user_id):
    a = person["address"]
    cc = person["creditCard"]
    return (f"user id {user_id}; email {person['email']}; ship to {a['streetAddress']}, {a['city']}, {a['state']} {a['zipCode']}, "
            f"{a['country']}; currency {person.get('userCurrency', 'USD')}; pay with card number {cc['creditCardNumber']}, "
            f"expiry {cc['creditCardExpirationMonth']}/{cc['creditCardExpirationYear']}, CVV {cc['creditCardCvv']}.")


def pick_question(people, user_id):
    """Returns (use_case, question_text)."""
    use_case = random.choices(list(USE_CASE_WEIGHTS), weights=list(USE_CASE_WEIGHTS.values()), k=1)[0]
    item = random.choice(QUESTIONS[use_case])
    text = item["q"]
    if "{customer}" in text:
        text = text.replace("{customer}", _customer_text(random.choice(people), user_id))
    return use_case, text


def golden_set():
    """All questions with expectations, for the offline eval."""
    return [dict(item, use_case=uc) for uc, items in QUESTIONS.items() for item in items]


if __name__ == "__main__":
    print(json.dumps(golden_set(), indent=1))
