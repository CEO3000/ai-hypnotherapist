"""The app shown inside the practice's website page: only that website may show it, never the admin pages; clients'
links and the return from payment lead to the website page."""

from app import notify
from app.payments import StripePayments

WEBSITE = "https://www.example.com"
PAGE = f"{WEBSITE}/ai-hypnotherapist"


def test_by_default_no_other_site_can_show_the_app(client):
    page = client.get("/")
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


def test_only_the_practices_website_can_show_the_client_page_and_never_the_admin(make_client):
    client = make_client(embed_origins=(WEBSITE, "http://localhost:3100"))
    page = client.get("/")
    assert "x-frame-options" not in page.headers
    assert f"frame-ancestors 'self' {WEBSITE} http://localhost:3100;" in page.headers["content-security-policy"]
    for admin in ("/sign-in", "/admin", "/api/clinician/sessions", "/api/config"):
        response = client.get(admin, follow_redirects=False)
        assert response.headers["x-frame-options"] == "DENY", admin
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"], admin


def test_the_app_knows_which_websites_it_may_tell_about_the_open_session(client, make_client):
    assert client.get("/api/config").json()["embed_origins"] == []
    embedded = make_client(embed_origins=(WEBSITE,))
    assert embedded.get("/api/config").json()["embed_origins"] == [WEBSITE]


def test_clients_links_and_the_return_from_payment_lead_to_the_website_page(settings, monkeypatch):
    from dataclasses import replace
    on_website = replace(settings, client_page_url=PAGE)
    message = notify.session_ready_email(on_website, to="sam@example.com", session_id="s" * 32, days=7)
    text = message.get_content()
    assert f"{PAGE}#s={'s' * 32}\n" in text
    assert f"{settings.public_url}/api/sessions/{'s' * 32}/download\n" in text  # the file itself: from the app
    assert replace(settings, client_page_url="").client_page() == f"{settings.public_url}/"

    sent = {}

    class Page:
        def to_dict(self):
            return {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"}

    payments = StripePayments(secret_key="sk_test_x", webhook_secret="whsec_x", client_page=on_website.client_page())
    monkeypatch.setattr(payments.client.v1.checkout.sessions, "create", lambda params: sent.update(params) or Page())
    payments.checkout(session_id="S" * 32, purpose="consultation", item="x", amount_pence=500, email=None,
                      save_card=False)
    assert sent["success_url"] == f"{PAGE}?checkout={{CHECKOUT_SESSION_ID}}#s={'S' * 32}"
    assert sent["cancel_url"] == f"{PAGE}#s={'S' * 32}"
