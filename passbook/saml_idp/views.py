"""passbook SAML IDP Views"""
from logging import getLogger

from django.contrib.auth import logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render, reverse
from django.utils.datastructures import MultiValueDictKeyError
from django.views import View

from passbook.lib.config import CONFIG
# from django.utils.html import escape
# from django.utils.translation import ugettext as _
from passbook.lib.mixins import CSRFExemptMixin
# from passbook.core.models import Event, Setting, UserAcquirableRelationship
from passbook.lib.utils.template import render_to_string
# from passbook.core.views.common import ErrorResponseView
# from passbook.core.views.settings import GenericSettingView
from passbook.saml_idp import exceptions, registry
from passbook.saml_idp.models import SAMLProvider

# from OpenSSL.crypto import FILETYPE_PEM
# from OpenSSL.crypto import Error as CryptoError
# from OpenSSL.crypto import load_certificate


LOGGER = getLogger(__name__)
URL_VALIDATOR = URLValidator(schemes=('http', 'https'))


def _generate_response(request, processor, remote):
    """Generate a SAML response using processor and return it in the proper Django
    response."""
    try:
        ctx = processor.generate_response()
        ctx['remote'] = remote
    except exceptions.UserNotAuthorized:
        return render(request, 'saml/idp/invalid_user.html')

    return render(request, 'saml/idp/login.html', ctx)


def render_xml(request, template, ctx):
    """Render template with content_type application/xml"""
    return render(request, template, context=ctx, content_type="application/xml")


class LoginBeginView(CSRFExemptMixin, View):
    """Receives a SAML 2.0 AuthnRequest from a Service Provider and
    stores it in the session prior to enforcing login."""

    def dispatch(self, request):
        if request.method == 'POST':
            source = request.POST
        else:
            source = request.GET
        # Store these values now, because Django's login cycle won't preserve them.

        try:
            request.session['SAMLRequest'] = source['SAMLRequest']
        except (KeyError, MultiValueDictKeyError):
            return HttpResponseBadRequest('the SAML request payload is missing')

        request.session['RelayState'] = source.get('RelayState', '')
        return redirect(reverse('passbook_saml_idp:saml_login_process'))


class RedirectToSPView(View):
    """Return autosubmit form"""

    def get(self, request, acs_url, saml_response, relay_state):
        """Return autosubmit form"""
        return render(request, 'core/autosubmit_form.html', {
            'url': acs_url,
            'attrs': {
                'SAMLResponse': saml_response,
                'RelayState': relay_state
            }
        })


class LoginProcessView(View):
    """Processor-based login continuation.
    Presents a SAML 2.0 Assertion for POSTing back to the Service Provider."""

    def dispatch(self, request):
        LOGGER.debug("Request: %s", request)
        proc, provider = registry.find_processor(request)
        # Check if user has access
        access = True
        # if provider.productextensionsaml2_set.exists() and \
        #         provider.productextensionsaml2_set.first().product_set.exists():
        #     # Only check if there is a connection from OAuth2 Application to product
        #     product = provider.productextensionsaml2_set.first().product_set.first()
        #     relationship = UserAcquirableRelationship.objects.
        # filter(user=request.user, model=product)
        #     # Product is invitation_only = True and no relation with user exists
        #     if product.invitation_only and not relationship.exists():
        #         access = False
        # Check if we should just autosubmit
        if provider.skip_authorization and access:
            # full_res = _generate_response(request, proc, provider)
            ctx = proc.generate_response()
            # User accepted request
            # Event.create(
            #     user=request.user,
            #     message=_('You authenticated %s (via SAML) (skipped Authz)' % provider.name),
            #     request=request,
            #     current=False,
            #     hidden=True)
            return RedirectToSPView.as_view()(
                request=request,
                acs_url=ctx['acs_url'],
                saml_response=ctx['saml_response'],
                relay_state=ctx['relay_state'])
        if request.method == 'POST' and request.POST.get('ACSUrl', None) and access:
            # User accepted request
            # Event.create(
            #     user=request.user,
            #     message=_('You authenticated %s (via SAML)' % provider.name),
            #     request=request,
            #     current=False,
            #     hidden=True)
            return RedirectToSPView.as_view()(
                request=request,
                acs_url=request.POST.get('ACSUrl'),
                saml_response=request.POST.get('SAMLResponse'),
                relay_state=request.POST.get('RelayState'))
        try:
            full_res = _generate_response(request, proc, provider)
            # if not access:
            #     LOGGER.warning("User '%s' has no invitation to '%s'", request.user, product)
            #     messages.error(request, "You have no access to '%s'" % product.name)
            #     raise Http404
            return full_res
        except exceptions.CannotHandleAssertion as exc:
            LOGGER.debug(exc)
            # return ErrorResponseView.as_view()(request, str(exc))


class LogoutView(CSRFExemptMixin, View):
    """Allows a non-SAML 2.0 URL to log out the user and
    returns a standard logged-out page. (SalesForce and others use this method,
    though it's technically not SAML 2.0)."""

    def get(self, request):
        """Perform logout"""
        logout(request)

        redirect_url = request.GET.get('redirect_to', '')

        try:
            URL_VALIDATOR(redirect_url)
        except ValidationError:
            pass
        else:
            return redirect(redirect_url)

        return render(request, 'saml/idp/logged_out.html')


class SLOLogout(CSRFExemptMixin, LoginRequiredMixin, View):
    """Receives a SAML 2.0 LogoutRequest from a Service Provider,
    logs out the user and returns a standard logged-out page."""

    def post(self, request):
        """Perform logout"""
        request.session['SAMLRequest'] = request.POST['SAMLRequest']
        # TODO: Parse SAML LogoutRequest from POST data, similar to login_process().
        # TODO: Add a URL dispatch for this view.
        # TODO: Modify the base processor to handle logouts?
        # TODO: Combine this with login_process(), since they are so very similar?
        # TODO: Format a LogoutResponse and return it to the browser.
        # XXX: For now, simply log out without validating the request.
        logout(request)
        return render(request, 'saml/idp/logged_out.html')


class DescriptorDownloadView(View):
    """Replies with the XML Metadata IDSSODescriptor."""

    def get(self, request, application_id):
        """Replies with the XML Metadata IDSSODescriptor."""
        application = get_object_or_404(SAMLProvider, pk=application_id)
        entity_id = CONFIG.y('saml_idp.issuer')
        slo_url = request.build_absolute_uri(reverse('passbook_saml_idp:saml_logout'))
        sso_url = request.build_absolute_uri(reverse('passbook_saml_idp:saml_login_begin'))
        pubkey = application.signing_cert
        ctx = {
            'entity_id': entity_id,
            'cert_public_key': pubkey,
            'slo_url': slo_url,
            'sso_url': sso_url
        }
        metadata = render_to_string('saml/xml/metadata.xml', ctx)
        response = HttpResponse(metadata, content_type='application/xml')
        response['Content-Disposition'] = 'attachment; filename="passbook_metadata.xml'
        return response


# class IDPSettingsView(GenericSettingView):
#     """IDP Settings"""

#     form = IDPSettingsForm
#     template_name = 'saml/idp/settings.html'

#     def dispatch(self, request, *args, **kwargs):
#         self.extra_data['metadata'] = escape(descriptor(request).content.decode('utf-8'))

#         # Show the certificate fingerprint
#         sha1_fingerprint = _('<failed to parse certificate>')
#         try:
#             cert = load_certificate(FILETYPE_PEM, CONFIG.y('saml_idp.certificate'))
#             sha1_fingerprint = cert.digest("sha1")
#         except CryptoError:
#             pass
#         self.extra_data['fingerprint'] = sha1_fingerprint
#         return super().dispatch(request, *args, **kwargs)
