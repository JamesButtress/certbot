"""Certbot Route53 authenticator plugin."""
import collections
import logging
import time
import os

from typing import Any
from typing import DefaultDict
from typing import Dict
from typing import List

import boto3
from botocore.exceptions import ClientError
from botocore.exceptions import NoCredentialsError

from acme.challenges import ChallengeResponse
from certbot import errors
from certbot.achallenges import AnnotatedChallenge
from certbot.plugins import dns_common

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "To use certbot-dns-route53, configure credentials as described at "
    "https://boto3.readthedocs.io/en/latest/guide/configuration.html#best-practices-for-configuring-credentials "  # pylint: disable=line-too-long
    "and add the necessary permissions for Route53 access.")


class Authenticator(dns_common.DNSAuthenticator):
    """Route53 Authenticator

    This authenticator solves a DNS01 challenge by uploading the answer to AWS
    Route53.
    """

    description = ("Obtain certificates using a DNS TXT record (if you are using AWS Route53 for "
                   "DNS).")
    ttl = 10

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.credentials = None
        self.credentials_file = None
        self.credentials_profile = None
        self.credentials_key_id = None
        self.credentials_access_key = None
        self.r53 = None
        # self.r53 = boto3.client("route53")
        self._resource_records: DefaultDict[str, List[Dict[str, str]]] = \
            collections.defaultdict(list)

    @classmethod
    def add_parser_arguments(cls, add):  # pylint: disable=arguments-differ
        super(Authenticator, cls).add_parser_arguments(add)
        add('credentials', help='route53 credentials file.')
        add('credentials-profile', help='profile to us from credentials file.')
        add('credentials-key-id', help='aws_access_key_id to use. overrides file and profile.')
        add('credentials-access_key', help='aws_secret_access_key to use. overrides file and profile.')


    def more_info(self) -> str:
        return "Solve a DNS01 challenge using AWS Route53"

    def _setup_credentials(self) -> None:
        self.credentials_file = self.conf('credentials')
        self.credentials_profile = self.conf('credentials-profile')
        self.credentials_key_id = self.conf('credentials-key-id')
        self.credentials_access_key = self.conf('credentials-access_key')

        if self.credentials_access_key != None and self.credentials_key_id != None:
            #logger.info('mcdebug key [%s][%s]', self.credentials_key_id, self.credentials_access_key)
            self.r53 = boto3.client(
                "route53",
                aws_access_key_id=self.credentials_key_id,
                aws_secret_access_key=self.credentials_access_key
            )
        else:
            if self.credentials_file != None:
                #logger.info('mcdebug file %s', self.credentials_file)
                os.environ['AWS_SHARED_CREDENTIALS_FILE'] = self.credentials_file

            if self.credentials_profile != None:
                #logger.info('mcdebug profile %s', self.credentials_profile)
                session = boto3.Session(profile_name=self.credentials_profile)
                self.r53 = session.client("route53")
            else:
                #logger.info('mcdebug boto3 finding credentials')
                self.r53 = boto3.client("route53")

    def _perform(self, domain: str, validation_name: str, validation: str) -> None:
        pass

    def perform(self, achalls: List[AnnotatedChallenge]) -> List[ChallengeResponse]:
        self._setup_credentials()
        self._attempt_cleanup = True

        try:
            change_ids = [
                self._change_txt_record("UPSERT",
                  achall.validation_domain_name(achall.domain),
                  achall.validation(achall.account_key))
                for achall in achalls
            ]

            for change_id in change_ids:
                self._wait_for_change(change_id)
        except (NoCredentialsError, ClientError) as e:
            logger.debug('Encountered error during perform: %s', e, exc_info=True)
            raise errors.PluginError("\n".join([str(e), INSTRUCTIONS]))
        return [achall.response(achall.account_key) for achall in achalls]

    def _cleanup(self, domain: str, validation_name: str, validation: str) -> None:
        try:
            self._change_txt_record("DELETE", validation_name, validation)
        except (NoCredentialsError, ClientError) as e:
            logger.debug('Encountered error during cleanup: %s', e, exc_info=True)

    def _find_zone_id_for_domain(self, domain: str) -> str:
        """Find the zone id responsible a given FQDN.

           That is, the id for the zone whose name is the longest parent of the
           domain.
        """
        paginator = self.r53.get_paginator("list_hosted_zones")
        zones = []
        target_labels = domain.rstrip(".").split(".")
        for page in paginator.paginate():
            for zone in page["HostedZones"]:
                if zone["Config"]["PrivateZone"]:
                    continue

                candidate_labels = zone["Name"].rstrip(".").split(".")
                if candidate_labels == target_labels[-len(candidate_labels):]:
                    zones.append((zone["Name"], zone["Id"]))

        if not zones:
            raise errors.PluginError(
                "Unable to find a Route53 hosted zone for {0}".format(domain)
            )

        # Order the zones that are suffixes for our desired to domain by
        # length, this puts them in an order like:
        # ["foo.bar.baz.com", "bar.baz.com", "baz.com", "com"]
        # And then we choose the first one, which will be the most specific.
        zones.sort(key=lambda z: len(z[0]), reverse=True)
        return zones[0][1]

    def _change_txt_record(self, action: str, validation_domain_name: str, validation: str) -> str:
        zone_id = self._find_zone_id_for_domain(validation_domain_name)

        rrecords = self._resource_records[validation_domain_name]
        challenge = {"Value": '"{0}"'.format(validation)}
        if action == "DELETE":
            # Remove the record being deleted from the list of tracked records
            rrecords.remove(challenge)
            if rrecords:
                # Need to update instead, as we're not deleting the rrset
                action = "UPSERT"
            else:
                # Create a new list containing the record to use with DELETE
                rrecords = [challenge]
        else:
            rrecords.append(challenge)

        response = self.r53.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Comment": "certbot-dns-route53 certificate validation " + action,
                "Changes": [
                    {
                        "Action": action,
                        "ResourceRecordSet": {
                            "Name": validation_domain_name,
                            "Type": "TXT",
                            "TTL": self.ttl,
                            "ResourceRecords": rrecords,
                        }
                    }
                ]
            }
        )
        return response["ChangeInfo"]["Id"]

    def _wait_for_change(self, change_id: str) -> None:
        """Wait for a change to be propagated to all Route53 DNS servers.
           https://docs.aws.amazon.com/Route53/latest/APIReference/API_GetChange.html
        """
        for unused_n in range(0, 120):
            response = self.r53.get_change(Id=change_id)
            if response["ChangeInfo"]["Status"] == "INSYNC":
                return
            time.sleep(5)
        raise errors.PluginError(
            "Timed out waiting for Route53 change. Current status: %s" %
            response["ChangeInfo"]["Status"])
