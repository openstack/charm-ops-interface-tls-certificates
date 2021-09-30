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

import unittest
import json

from ops.charm import CharmBase
from ops import testing
from ops import model
from ops import framework

import interface_tls_certificates.ca_client as ca_client

from test.ca_client_test_data import (
    TEST_RELATION_DATA,
    get_multi_rq_relation_data_server,
    get_multi_rq_relation_data_client)


class TestCAClient(unittest.TestCase):

    def setUp(self):
        self.harness = testing.Harness(CharmBase, meta='''
            name: myserver
            peers:
              ca-client:
                interface: tls-certificates
        ''')

        self.harness.begin()
        self.ca_client = ca_client.CAClient(self.harness.charm, 'ca-client')

    def test_ca_available(self):

        class TestReceiver(framework.Object):

            def __init__(self, parent, key):
                super().__init__(parent, key)
                self.observed_events = []

            def on_ca_available(self, event):
                self.observed_events.append(event)

        receiver = TestReceiver(self.harness.framework, 'receiver')
        self.harness.framework.observe(self.ca_client.on.ca_available,
                                       receiver.on_ca_available)

        relation_id = self.harness.add_relation('ca-client', 'easyrsa')
        self.assertTrue(self.ca_client.is_joined)

        self.harness.add_relation_unit(relation_id, 'easyrsa/0')
        self.harness.update_relation_data(relation_id, 'easyrsa/0',
                                          {'ingress-address': '192.0.2.2'})

        self.assertTrue(len(receiver.observed_events) == 1)
        self.assertIsInstance(receiver.observed_events[0],
                              ca_client.CAAvailable)

    def test_request_server_certificate(self):
        relation_id = self.harness.add_relation('ca-client', 'easyrsa')

        self.harness.update_relation_data(
            relation_id, 'myserver/0', {'ingress-address': '192.0.2.1'})

        self.harness.add_relation_unit(relation_id, 'easyrsa/0')
        self.harness.update_relation_data(relation_id, 'easyrsa/0',
                                          {'ingress-address': '192.0.2.2'})
        rel = self.harness.charm.model.get_relation('ca-client')

        # Cannot obtain {certificate, key, ca_certificate} before
        # a request is made.
        with self.assertRaises(ca_client.CAClientError) as cm:
            self.ca_client.certificate
        self.assertIsInstance(cm.exception.status, model.BlockedStatus)

        with self.assertRaises(ca_client.CAClientError) as cm:
            self.ca_client.key
        self.assertIsInstance(cm.exception.status, model.BlockedStatus)

        with self.assertRaises(ca_client.CAClientError) as cm:
            self.ca_client.ca_certificate
        self.assertIsInstance(cm.exception.status, model.BlockedStatus)

        example_hostname = 'myserver.example'
        sans = [example_hostname, '192.0.2.1']
        self.ca_client.request_server_certificate(example_hostname, sans)

        server_data = rel.data[self.harness.charm.model.unit]
        self.assertEqual(server_data['common_name'], example_hostname)
        self.assertEqual(server_data['sans'], json.dumps(sans))
        self.assertEqual(server_data['unit_name'],
                         self.harness.charm.model.unit.name)

        # Waiting for more relation data now - check for WaitingStatus
        # in the exception.
        with self.assertRaises(ca_client.CAClientError) as cm:
            self.ca_client.certificate
        self.assertIsInstance(cm.exception.status, model.WaitingStatus)

        with self.assertRaises(ca_client.CAClientError) as cm:
            self.ca_client.key
        self.assertIsInstance(cm.exception.status, model.WaitingStatus)

        with self.assertRaises(ca_client.CAClientError) as cm:
            self.ca_client.ca_certificate
        self.assertIsInstance(cm.exception.status, model.WaitingStatus)

        # Simulate a change and make sure it propagates to relation
        # data correctly.
        new_example_hostname = 'myserver1.example'
        new_sans = [new_example_hostname, '192.0.2.10']
        self.ca_client.request_server_certificate(new_example_hostname,
                                                  new_sans)
        self.assertEqual(server_data['common_name'], new_example_hostname)
        self.assertEqual(server_data['sans'], json.dumps(new_sans))
        self.assertEqual(server_data['unit_name'],
                         self.harness.charm.model.unit.name)

    def prepare_on_relation_changed_test(self, client_data, server_data):

        class TestReceiver(framework.Object):

            def __init__(self, parent, key):
                super().__init__(parent, key)
                self.observed_events = {
                    'legacy': [],
                    'server': [],
                    'application': [],
                    'client': []}

            def on_tls_config_ready(self, event):
                self.observed_events['legacy'].append(event)

            def on_tls_app_config_ready(self, event):
                self.observed_events['application'].append(event)

            def on_tls_server_config_ready(self, event):
                self.observed_events['server'].append(event)

            def on_tls_client_config_ready(self, event):
                self.observed_events['client'].append(event)

        self.receiver = TestReceiver(self.harness.framework, 'receiver')
        self.harness.framework.observe(
            self.ca_client.on.tls_config_ready,
            self.receiver.on_tls_config_ready)
        self.harness.framework.observe(
            self.ca_client.on.tls_app_config_ready,
            self.receiver.on_tls_app_config_ready)
        self.harness.framework.observe(
            self.ca_client.on.tls_client_config_ready,
            self.receiver.on_tls_server_config_ready)
        self.harness.framework.observe(
            self.ca_client.on.tls_server_config_ready,
            self.receiver.on_tls_client_config_ready)

        self.relation_id = self.harness.add_relation('ca-client', 'easyrsa')
        self.harness.update_relation_data(
            self.relation_id, 'myserver/0',
            {'ingress-address': '10.209.240.176'})

        self.harness.add_relation_unit(self.relation_id, 'easyrsa/0')
        self.harness.update_relation_data(self.relation_id, 'easyrsa/0',
                                          {'ingress-address': '192.0.2.2'})
        self.harness.update_relation_data(
            self.relation_id,
            'myserver/0',
            client_data)
        # The certificates and a key were generated once for the purposes
        # of creating an example. They are not used anywhere in
        # a production or test system.
        self.harness.update_relation_data(self.relation_id, 'easyrsa/0',
                                          server_data)

    def test__on_relation_changed(self):
        self.prepare_on_relation_changed_test(
            {
                'ingress-address': '10.209.240.176',
                'common_name': '10.209.240.176',
                'sans': json.dumps(['10.209.240.176'])},
            TEST_RELATION_DATA)

        self.assertTrue(len(self.receiver.observed_events['legacy']) == 1)
        self.assertIsInstance(self.receiver.observed_events['legacy'][0],
                              ca_client.TLSConfigReady)

        # Validate that the properties of certs and keys match
        # the ones exposed.
        self.assertEqual(self.ca_client.ca_certificate.serial_number,
                         370671393612319950261394837222550598495379101011)
        self.assertEqual(self.ca_client.certificate.serial_number, 2)
        self.assertEqual(self.ca_client.key.public_key().public_numbers().e,
                         65537)
        self.assertTrue(self.ca_client.is_ready)

    def test__on_relation_changed_ca_chain(self):
        self.prepare_on_relation_changed_test(
            get_multi_rq_relation_data_client(),
            get_multi_rq_relation_data_server())
        self.assertEqual(
            self.ca_client.ca_certificate.serial_number,
            192863404968765739414495968089296236155169528104)
        self.assertEqual(
            self.ca_client.root_ca_chain.serial_number,
            364727974956649209413854240588010868175254941108)

        # Validate that the properties of certs and keys match
        # the ones exposed.
        self.assertEqual(
            self.ca_client.ca_certificate.serial_number,
            192863404968765739414495968089296236155169528104)
        self.assertEqual(
            self.ca_client.root_ca_chain.serial_number,
            364727974956649209413854240588010868175254941108)

    def test__on_relation_changed_app_cert(self):
        self.prepare_on_relation_changed_test(
            get_multi_rq_relation_data_client(),
            get_multi_rq_relation_data_server())
        self.assertTrue(len(self.receiver.observed_events['application']) == 1)
        self.assertIsInstance(self.receiver.observed_events['application'][0],
                              ca_client.TLSConfigReady)

        # Validate that the properties of certs and keys match
        # the ones exposed.
        self.assertEqual(
            self.ca_client.application_certificate.serial_number,
            545775603358522149900500872541885055132503562027)
        self.assertEqual(
            self.ca_client.application_key.public_key().public_numbers().e,
            65537)
        self.assertTrue(self.ca_client.is_application_cert_ready)

    def test__on_relation_changed_server_cert(self):
        self.prepare_on_relation_changed_test(
            get_multi_rq_relation_data_client(),
            get_multi_rq_relation_data_server())
        self.assertTrue(len(self.receiver.observed_events['server']) == 1)
        self.assertIsInstance(self.receiver.observed_events['server'][0],
                              ca_client.TLSConfigReady)

        # Validate that the properties of certs and keys match
        # the ones exposed.
        self.assertEqual(
            self.ca_client.server_certificate.serial_number,
            278879076309639781313885930372307854071574482585)
        self.assertEqual(
            self.ca_client.server_key.public_key().public_numbers().e,
            65537)
        self.assertTrue(self.ca_client.is_application_cert_ready)
        certs = self.ca_client.server_certs
        self.assertEqual(
            certs['server1']['cert'].serial_number,
            278879076309639781313885930372307854071574482585)
        self.assertEqual(
            certs['server2']['cert'].serial_number,
            500144078276114303654132221008280693054965976604)

    def test__on_relation_changed_client_cert(self):
        self.prepare_on_relation_changed_test(
            get_multi_rq_relation_data_client(),
            get_multi_rq_relation_data_server())
        self.assertTrue(len(self.receiver.observed_events['client']) == 1)
        self.assertIsInstance(self.receiver.observed_events['client'][0],
                              ca_client.TLSConfigReady)

        # Validate that the properties of certs and keys match
        # the ones exposed.
        self.assertEqual(
            self.ca_client.client_certificate.serial_number,
            317090354556363424911379458510806571627459623032)
        self.assertEqual(
            self.ca_client.client_key.public_key().public_numbers().e,
            65537)
        self.assertTrue(self.ca_client.is_application_cert_ready)
        certs = self.ca_client.client_certs
        self.assertEqual(
            certs['client1']['cert'].serial_number,
            317090354556363424911379458510806571627459623032)
        self.assertEqual(
            certs['client2']['cert'].serial_number,
            554251068938213429919465619370496662368340363424)


if __name__ == "__main__":
    unittest.main()
