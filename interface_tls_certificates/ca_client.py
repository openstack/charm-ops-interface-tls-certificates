# Copyright 2020 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Implements the requires side handling of the 'tls-certificates' interface.

`CAClient`_ is the type providing integration with a certificate authority
charm providing the tls-certificates interface. CAClient can be used to
request server, client or application certificates. Only one application
certificate may be requested but multiple client and server requests are
supported.

The simplest request is one server certificate::


    from pathlib import Path
    from ops.charm import CharmBase
    from ops.lib.tls_certificates.ca_client import CAClient

    class MyCharm(CharmBase):

        TLS_CONFIG_PATH = Path("/tls/config/path/for/your/app")
        TLS_KEY_PATH = TLS_CONFIG_PATH / 'key.pem'
        TLS_CERT_PATH = TLS_CONFIG_PATH / 'cert.pem'
        TLS_CA_CERT_PATH = TLS_CONFIG_PATH / 'ca.pem'

        def __init__(self, *args):
            super().__init__(*args)
            self.ca_client = CAClient(self, 'ca-client')
            self.framework.observe(self.ca_client.on.tls_config_ready,
                                   self._on_tls_config_ready)
            self.framework.observe(self.ca_client.on.ca_available,
                                   self._on_ca_available)

        def _on_ca_available(self, event):
            # Obtain a common name and a list of subject alternative names to
            # place into certificates to be generated by a certificate
            # authority and expose them to the CA.
            self.ca_client.request_server_certificate(common_name, sans)

        def _on_tls_config_ready(self, event):
            # When TLS config is ready, a CA certificate, requested certificate
            # and key will be available from an instance of CAClient. It can be
            # written to the target files and used by the target application.
            self.TLS_KEY_PATH.write_bytes(self.ca_client.key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))
            self.TLS_CERT_PATH.write_bytes(
                self.ca_client.certificate.public_bytes(encoding=serialization.Encoding.PEM))
            self.TLS_CA_CERT_PATH.write_bytes(
                self.ca_client.ca_certificate.public_bytes(encoding=serialization.Encoding.PEM))
            # Reconfigure and reload your application after writing to files.


To request an additional server certificate
`self.ca_client.request_server_certificate` can be called again.

To request a client certificate
`self.ca_client.request_client_certificate` should be used and the charm should
obsever the `tls_client_config_ready` event.

Finally to request an application certificate (certificate which works on all
units of an application by combining all the sans) the use
`self.ca_client.request_application_certificate` and observer
`tls_client_config_ready`.
"""


import functools
import json
import logging

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509 import load_pem_x509_certificate

from ops.framework import (
    Object,
    EventBase,
    ObjectEvents,
    EventSource,
    StoredState
)
from ops.model import ModelError, BlockedStatus, WaitingStatus
logger = logging.getLogger(__name__)


class TLSCertificatesError(ModelError):
    """A base class for all errors raised by interface-tls-certificates.

    The error provides the attribute self.status to indicate what status and
    message the Unit should use based on the status of this relation. For
    example, if there is no relation to a CA, it will raise a
    BlockedStatus('Missing relation <relation-name>')
    """

    def __init__(self, kind, message, relation_name):
        super().__init__()
        self.status = kind('{}: {}'.format(message, relation_name))


class CAClientError(TLSCertificatesError):
    """An error specific to the CAClient class"""


class CAAvailable(EventBase):
    """Event emitted by CAClient.on.ca_available.

    This event will be emitted by CAClient when a new unit of a CA charm
    joins the relation. If there are multiple units joining one by one,
    multiple events will be triggered.

    The expected response from a handler of that event is to request a
    certificate from the CA via the API provided by CAClient.
    """


class TLSConfigReady(EventBase):
    """Event emitted by CAClient.on.tls_config_ready.

    This event will be emitted by CAClient when a remote CA unit.

    The expected response from a handler of that event is to request a
    certificate from the CA via the API provided by CAClient.
    """


class CAClientEvents(ObjectEvents):
    """Events emitted by the CAClient class."""

    ca_available = EventSource(CAAvailable)
    tls_config_ready = EventSource(TLSConfigReady)
    tls_app_config_ready = EventSource(TLSConfigReady)
    tls_server_config_ready = EventSource(TLSConfigReady)
    tls_client_config_ready = EventSource(TLSConfigReady)


class CAClient(Object):
    """Provides a client type that handles the interaction with CA charms.

    It mainly provides:

    * an indication that a CA unit is available to accept requests
      for certificates;
    * a method to provide details (CN, SANs) to the CA for
      generating certificates;
    * an indication that a certificate and a key have been generated;
    * a way to retrieve the generated certificate and key as well as
      a CA certificate.
    """

    on = CAClientEvents()
    _stored = StoredState()

    REQUEST_KEYS = {
        'legacy': '',
        'server': 'cert_requests',
        'client': 'client_cert_requests',
        'application': 'application_cert_requests'}

    PROCESSED_KEYS = {
        'legacy': '',
        'server': 'processed_requests',
        'client': 'processed_client_requests',
        'application': 'processed_application_requests'}

    def __init__(self, charm, relation_name):
        """
        :param charm: the charm object to be used as a parent object.
        :type charm: :class: `ops.charm.CharmBase`
        """
        super().__init__(charm, relation_name)
        self._relation_name = self.relation_name = relation_name
        self._common_name = None
        self._sans = None
        self._munged_name = self.model.unit.name.replace("/", "_")
        self._stored.set_default(
            ca_certificate=None,
            key=None,
            certificate=None,
            root_ca_chain=None,
            legacy=None,
            client=None,
            server=None,
            application=None)
        self.framework.observe(charm.on[relation_name].relation_joined,
                               self._on_relation_joined)
        self.framework.observe(charm.on[relation_name].relation_changed,
                               self._on_relation_changed)
        self.ready_events = {
            'legacy': self.on.tls_config_ready,
            'server': self.on.tls_server_config_ready,
            'client': self.on.tls_client_config_ready,
            'application': self.on.tls_app_config_ready}

    def _on_relation_joined(self, event):
        self.on.ca_available.emit()

    @property
    def is_joined(self):
        """Whether this charm has joined the relation."""
        rel = self.framework.model.get_relation(self._relation_name)
        return rel is not None

    @property
    def is_ready(self):
        """Whether this charm has fulfilled the legacy certificate requests."""
        return self._is_cert_ready('legacy')

    def _is_cert_ready(self, cert_type):
        """Check whether there is a response for the cert_type.

        :param cert_type: Certificate type
        :type cert_type: str
        :returns: Whether there is a response for the cert_type
        :rtype: bool
        """
        try:
            return all([
                self.ca_certificate,
                getattr(self._stored, cert_type)])
        except CAClientError:
            return False

    @property
    def is_application_cert_ready(self):
        """Have application certificate requests been fulfilled.

        :returns: Whether requests have been fulfilled
        :rtype: bool
        """
        return self._is_cert_ready('application')

    @property
    def is_server_cert_ready(self):
        """Have server certificate requests been fulfilled.

        :returns: Whether requests have been fulfilled
        :rtype: bool
        """
        return self._is_cert_ready('server')

    @property
    def is_client_cert_ready(self):
        """Have client certificate requests been fulfilled.

        :returns: Whether requests have been fulfilled
        :rtype: bool
        """
        return self._is_cert_ready('client')

    def _get_certs_and_keys(self, request_type):
        """For the given request_type return the certs and keys from the CA.

        :param request_type: Certificate type
        :type request_type: str
        :returns: Dictionary keyed on CN of certs and keys
        :rtype: Dict[str, Union[
            default_backend.openssl.rsa.openssl.rsa._RSAPrivateKey,
            default_backend.openssl.openssl.x509._Certificate]]
        :raises: CAClientError
        """
        if not self._is_certificate_requested(request_type):
            raise CAClientError(BlockedStatus,
                                'a certificate request has not been sent',
                                self._relation_name)
        crypto_data = getattr(self._stored, request_type)
        if not crypto_data:
            raise CAClientError(
                WaitingStatus,
                'a {} has not been obtained yet.'.format(request_type),
                self._relation_name)
        pem_data = {}
        for cn, data in crypto_data.items():
            pem_data[cn] = {
                'key': load_pem_private_key(
                    data['key'].encode('utf-8'),
                    password=None,
                    backend=default_backend()),
                'cert': load_pem_x509_certificate(
                    data['cert'].encode('utf-8'),
                    backend=default_backend())}
        if pem_data:
            default_entry = sorted(pem_data.keys())[0]
            pem_data['default'] = pem_data[default_entry]
        return pem_data

    def _get_certificate(self, txt_cert):
        """Return the certificate object for the given string.

        :param txt_cert: Text of certificate.
        :type txt_cert: str
        :returns: Certificate
        :rtype: default_backend.openssl.x509._Certificate
        :raises: CAClientError
        """
        if not self._any_certificate_requested():
            raise CAClientError(BlockedStatus,
                                'a certificate request has not been sent',
                                self._relation_name)
        if txt_cert is None:
            raise CAClientError(WaitingStatus,
                                'certificate has not been obtained yet.',
                                self._relation_name)
        return load_pem_x509_certificate(
            txt_cert.encode('utf-8'),
            backend=default_backend())

    @property
    def ca_certificate(self):
        """Return the CA certificate.

        :returns: Certificate
        :rtype: default_backend.openssl.x509._Certificate
        :raises: CAClientError
        """
        return self._get_certificate(self._stored.ca_certificate)

    @property
    def root_ca_chain(self):
        """Return the CA chain certificate.

        :returns: Certificate
        :rtype: default_backend.openssl.x509._Certificate
        :raises: CAClientError
        """
        return self._get_certificate(self._stored.root_ca_chain)

    @property
    def certificate(self):
        """Certificate from CA for certificate request using legacy method.

        :returns: Certificate
        :rtype: default_backend.openssl.x509._Certificate
        :raises: CAClientError
        """
        return self._get_certs_and_keys(
            'legacy')[self._legacy_request_cn]['cert']

    @property
    def server_certificate(self):
        """Certificate from CA for server certificate request.

        This method should not be used if multiple certificates were requested.
        Instead use self.server_certs()

        :returns: Certificate
        :rtype: default_backend.openssl.x509._Certificate
        :raises: CAClientError
        """
        return self._get_certs_and_keys('server')['default']['cert']

    @property
    def client_certificate(self):
        """Certificate from CA for client certificate request.

        This method should not be used if multiple certificates were requested.
        Instead use self.client_certs()

        :returns: Certificate
        :rtype: default_backend.openssl.x509._Certificate
        :raises: CAClientError
        """
        return self._get_certs_and_keys('client')['default']['cert']

    @property
    def application_certificate(self):
        """Certificate from CA for application certificate request.

        This method should not be used if multiple certificates were requested.
        Instead use self.application_certs()

        :returns: Certificate
        :rtype: default_backend.openssl.x509._Certificate
        :raises: CAClientError
        """
        return self._get_certs_and_keys('application')['default']['cert']

    @property
    def key(self):
        """Key from CA for certificate request using legacy method.

        :returns: Key
        :rtype: default_backend.openssl.rsa.openssl.rsa._RSAPrivateKey
        :raises: CAClientError
        """
        return self._get_certs_and_keys(
            'legacy')[self._legacy_request_cn]['key']

    @property
    def server_key(self):
        """Key from CA for server certificate request.

        This method should not be used if multiple certificates were requested.
        Instead use self.server_certs()

        :returns: Key
        :rtype: default_backend.openssl.rsa.openssl.rsa._RSAPrivateKey
        :raises: CAClientError
        """
        return self._get_certs_and_keys('server')['default']['key']

    @property
    def client_key(self):
        """Key from CA for client certificate request.

        This method should not be used if multiple certificates were requested.
        Instead use self.client_certs()

        :returns: Key
        :rtype: default_backend.openssl.rsa.openssl.rsa._RSAPrivateKey
        :raises: CAClientError
        """
        return self._get_certs_and_keys('client')['default']['key']

    @property
    def application_key(self):
        """Key from CA for application certificate request.

        This method should not be used if multiple certificates were requested.
        Instead use self.application_certs()

        :returns: Key
        :rtype: default_backend.openssl.rsa.openssl.rsa._RSAPrivateKey
        :raises: CAClientError
        """
        return self._get_certs_and_keys('application')['default']['key']

    @property
    def application_certs(self):
        """Application Certificates and keys returned by CA

        :returns: Dictionary keyed on CN of certs and keys
        :rtype: Dict[str, Union[
            default_backend.openssl.rsa.openssl.rsa._RSAPrivateKey,
            default_backend.openssl.openssl.x509._Certificate]]
        :raises: CAClientError
        """
        return self._get_certs_and_keys('application')

    @property
    def server_certs(self):
        """Server Certificates and keys returned by CA

        :returns: Dictionary keyed on CN of certs and keys
        :rtype: Dict[str, Union[
            default_backend.openssl.rsa.openssl.rsa._RSAPrivateKey,
            default_backend.openssl.openssl.x509._Certificate]]
        :raises: CAClientError
        """
        return self._get_certs_and_keys('server')

    @property
    def client_certs(self):
        """Client Certificates and keys returned by CA

        :returns: Dictionary keyed on CN of certs and keys
        :rtype: Dict[str, Union[
            default_backend.openssl.rsa.openssl.rsa._RSAPrivateKey,
            default_backend.openssl.openssl.x509._Certificate]]
        :raises: CAClientError
        """
        return self._get_certs_and_keys('client')

    @property
    def _legacy_request_cn(self):
        """The common name used for a certificate request using legacy method.

        :param common_name: Common name
        :type common_name: str
        """
        cn = None
        rel = self.framework.model.get_relation(self._relation_name)
        if rel:
            cn = rel.data[self.framework.model.unit].get('common_name')
        return cn

    def request_certificate(self, common_name, sans, certificate_type=None):
        """Request a new server certificate.

        If arguments have not changed from a previous request, then a different
        certificate will not be generated. This method can be useful if a list
        of SANS has changed during the lifetime of a charm and a new
        certificate needs to be generated.

        :param common_name: a new common name to use in a certificate.
        :type common_name: str
        :param sans: a list of Subject Alternative Names to use in a
            certificate.
        :type common_name: list(str)
        """
        key = self.REQUEST_KEYS[certificate_type]
        rel = self.framework.model.get_relation(self._relation_name)
        if rel is None:
            raise CAClientError(BlockedStatus, 'missing relation',
                                self._relation_name)
        logger.info(
            'Requesting a CA certificate. Common name: %s, SANS: %s',
            common_name,
            sans)
        requests = rel.data[self.model.unit].get(key, '{}')
        requests = json.loads(requests)
        requests[common_name] = {'sans': sans}
        rel.data[self.model.unit][key] = json.dumps(
            requests,
            sort_keys=True)
        rel_data = rel.data[self.model.unit]
        if certificate_type == 'server':
            # for backwards compatibility, request goes in its own fields
            rel_data['common_name'] = common_name
            rel_data['sans'] = json.dumps(sans)
        # Explicit set of unit_name needed to support use of
        # this interface in cross model contexts.
        rel_data['unit_name'] = self.model.unit.name

    request_server_certificate = functools.partialmethod(
        request_certificate,
        certificate_type='server')

    request_client_certificate = functools.partialmethod(
        request_certificate,
        certificate_type='client')

    request_application_certificate = functools.partialmethod(
        request_certificate,
        certificate_type='application')

    def _is_certificate_requested(self, request_type):
        """Has a request beed sent of this type.

        :param request_type: Certificate type
        :type request_type: str
        :returns: Whether a request has been sent.
        :rtype: bool
        """
        return bool(self._get_all_requests().get(request_type))

    def _any_certificate_requested(self):
        """Have any certificate requests been sent

        :returns: Whether a request has been sent.
        :rtype: bool
        """
        return any([i for i in self._get_all_requests().values()])

    def _get_legacy_response(self, remote_data):
        """Retrieve response from CA using legacy method.

        :param remote_data: Data returned by CA
        :type remote_data: ops.model.RelationDataContent
        :returns: Dict keyed on cn of key and cert
        :rtype: Dict[str, str]
        """
        certs_data = {}
        cert = remote_data.get(
            '{}.server.cert'.format(self._munged_name))
        key = remote_data.get(
            '{}.server.key'.format(self._munged_name))
        if all([self._legacy_request_cn, cert, key]):
            certs_data = {
                self._legacy_request_cn: {
                    'key': key,
                    'cert': cert}}
        return certs_data

    def _get_request_response(self, request_type, remote_data):
        """Retrieve response from CA using legacy method.

        :param remote_data: Data returned by CA
        :type remote_data: ops.model.RelationDataContent
        :returns: Dict keyed on cn of key and cert
        :rtype: Dict[str, str]
        """
        rq_key = self.PROCESSED_KEYS[request_type]
        certs_data = {}
        if rq_key:
            field = '{}.{}'.format(self._munged_name, rq_key)
            json_certs_data = remote_data.get(field)
            if json_certs_data:
                certs_data = json.loads(json_certs_data)
            # If a server cert was requested by the legacy top level mechanism
            # then make sure it is included in the server certs dict.
            if request_type == 'server':
                certs_data.update(self._get_legacy_response(remote_data))
        else:
            certs_data = self._get_legacy_response(remote_data)
        return certs_data

    def _store_certificates(self, request_type, crypto_data):
        """Store the response from the CA for the given request type.

        :param request_type: Certificate type
        :type request_type: str
        :param crypto_data: Data returned by CA for request. Expected to be:
                            crypto_data is in the for {'cn': {'cert': str,
                                                              'key': str}}
        :type crypto_data: Dict[str, Dict[str, str]]
        """
        setattr(self._stored, request_type, crypto_data)

    def _get_all_requests(self):
        """Get all the certificate requests this unit has made.

        :returns: Dict keyed on request type
                  {'application': { 'cn': {'cert':, 'key':}...
        :rtype: Dict[str, Dict[str, Dict[str, str]]]
        """
        requests = {}
        rel = self.framework.model.get_relation(self._relation_name)
        if rel is None:
            return requests
        unit_data = rel.data[self.framework.model.unit]
        for request_type, request_key in self.REQUEST_KEYS.items():
            if request_type == 'legacy':
                cn = unit_data.get('common_name')
                if cn:
                    requests[request_type] = {
                        cn: {
                            'sans': json.loads(unit_data.get('sans', '[]'))}}
            else:
                requests[request_type] = json.loads(
                    unit_data.get(request_key, '{}'))
        return requests

    def _valid_response(self, response):
        """Check if data from CA for request is valid.

        :param response: Certificate type
        :type response: Union[str, None]
        :returns: If response is valid
        :rtype: bool
        """
        if response:
            return all([response.get('cert'), response.get('key')])
        else:
            return False

    def _on_relation_changed(self, event):
        """Check if requests have been processed and emit events accorfdingly.


        Check which requests have been processes. If all requests of a
        particular type have been completed them emit the corresponding event.

        :raises: CAClientError
        """
        remote_data = event.relation.data[event.unit]
        ca = remote_data.get('ca')
        if not ca:
            return
        self._stored.ca_certificate = ca
        chain = remote_data.get('chain')
        if chain:
            self._stored.root_ca_chain = chain
        requests = self._get_all_requests()
        for request_type, request in requests.items():
            if not request:
                continue
            response = self._get_request_response(request_type, remote_data)
            if request_type == 'application':
                req_keys = ['app_data']
            else:
                req_keys = request.keys()
            for key in req_keys:
                if not self._valid_response(response.get(key)):
                    message = (
                        'A CA has not yet processed requests: {}'.format(key))
                    logger.info(message)
                    continue
            else:
                # All requests of this type have completed so emit the
                # corresponding event
                self._store_certificates(request_type, response)
                self.ready_events[request_type].emit()
