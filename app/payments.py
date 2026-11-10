"""Card payments through Stripe (the official stripe library, MIT licence).

Clients pay on Stripe's own secure payment page (Checkout), so the app never sees card details. A card the client
chose to save at the consultation is charged directly for the full session; if their bank wants them to confirm
that payment, they go to the payment page instead. A payment counts only once the app has checked it with Stripe
itself: when the client comes back from the payment page, and again when Stripe notifies the app (the webhook), so
closing the page too early loses nothing.
"""

import stripe


class PaymentUnavailable(RuntimeError):
    """Stripe could not be reached or refused the request (the client is asked to try again)."""


class CardNeedsCheckout(RuntimeError):
    """The saved card can't be charged without the client (for example their bank asks them to confirm)."""


class StripePayments:
    def __init__(self, *, secret_key: str, webhook_secret: str, client_page: str):
        self.client = stripe.StripeClient(secret_key)
        self.webhook_secret = webhook_secret
        self.client_page = client_page  # where the client comes back to (Settings.client_page)

    def checkout(self, *, session_id: str, purpose: str, item: str, amount_pence: int, email: str | None,
                 save_card: bool, locale: str = "en") -> dict:
        """A Stripe payment page for this payment: {"id", "url"}."""
        metadata = {"session_id": session_id, "purpose": purpose}
        intent = {"metadata": metadata, "description": item}
        params = {
            "mode": "payment",
            "payment_method_types": ["card"],  # cards (Apple Pay and Google Pay too): a card can be saved for later
            "line_items": [{"price_data": {"currency": "gbp", "product_data": {"name": item},
                                           "unit_amount": amount_pence}, "quantity": 1}],
            "success_url": f"{self.client_page}?checkout={{CHECKOUT_SESSION_ID}}#s={session_id}",
            "cancel_url": f"{self.client_page}#s={session_id}",
            "metadata": metadata,
            "locale": {"en": "en-GB"}.get(locale, locale),  # Stripe's page in the client's language
            "payment_intent_data": {**intent, "setup_future_usage": "off_session"} if save_card else intent,
        }
        if email:
            params["customer_email"] = email
        if save_card:
            params["customer_creation"] = "always"
        try:
            created = self.client.v1.checkout.sessions.create(params=params).to_dict()
        except stripe.StripeError as exc:
            raise PaymentUnavailable(str(exc)) from exc
        return {"id": created["id"], "url": created["url"]}

    def checkout_status(self, checkout_id: str) -> dict:
        """What Stripe says about a payment page: paid or not, the amount, and the saved card (if any)."""
        try:
            found = self.client.v1.checkout.sessions.retrieve(
                checkout_id, params={"expand": ["payment_intent"]}).to_dict()
        except stripe.StripeError as exc:
            raise PaymentUnavailable(str(exc)) from exc
        intent = found.get("payment_intent")
        intent = intent if isinstance(intent, dict) else {"id": intent}
        customer = found.get("customer")
        return {
            "paid": found.get("payment_status") == "paid",
            "amount_pence": found.get("amount_total"),
            "currency": found.get("currency"),
            "session_id": (found.get("metadata") or {}).get("session_id"),
            "purpose": (found.get("metadata") or {}).get("purpose"),
            "payment_intent": intent.get("id"),
            "customer": customer.get("id") if isinstance(customer, dict) else customer,
            "payment_method": intent.get("payment_method"),
        }

    def charge_saved_card(self, *, customer: str, payment_method: str, amount_pence: int, item: str,
                          session_id: str, purpose: str) -> str:
        """Charges the card the client saved; returns Stripe's payment reference."""
        try:
            intent = self.client.v1.payment_intents.create(params={
                "amount": amount_pence, "currency": "gbp", "customer": customer, "payment_method": payment_method,
                "off_session": True, "confirm": True, "description": item,
                "metadata": {"session_id": session_id, "purpose": purpose},
            }).to_dict()
        except stripe.CardError as exc:
            raise CardNeedsCheckout(str(exc)) from exc
        except stripe.StripeError as exc:
            raise PaymentUnavailable(str(exc)) from exc
        if intent.get("status") != "succeeded":
            raise CardNeedsCheckout(f"payment status {intent.get('status')}")
        return intent["id"]

    def event(self, payload: bytes, signature: str | None) -> dict:
        """A notification from Stripe, checked against the webhook's signing secret (raises ValueError if it isn't
        genuine)."""
        try:
            return self.client.construct_event(payload, signature, self.webhook_secret).to_dict()
        except (stripe.SignatureVerificationError, ValueError) as exc:
            raise ValueError("not a genuine Stripe notification") from exc
