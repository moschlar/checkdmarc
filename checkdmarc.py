#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Validates and parses SPF amd DMARC DNS records"""

from __future__ import unicode_literals, print_function

from sys import version_info
from re import compile
import json
from csv import DictWriter
from argparse import ArgumentParser
from os import path

try:
    from cStringIO import StringIO
except ImportError:
    from io import StringIO

import dns.resolver
import dns.exception
from pyleri import (Grammar,
                    Token,
                    Regex,
                    Sequence,
                    List,
                    Repeat
                    )

"""Copyright 2017 Sean Whalen

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License."""


# Python 2 comparability hack
if version_info[0] >= 3:
    unicode = str


__version__ = "1.0.1"


class DMARCException(Exception):
    """Raised when an error occurs when retrieving or parsing a DMARC record"""
    pass


class SPFException(Exception):
    """Raised when an error occurs when retrieving or parsing a SPF record"""
    pass


class SPFRecordNotFound(SPFException):
    """Raised when a SPF record could not be found"""
    pass


class SPFRecordInvalid(SPFException):
    """Raised when a SPF record is found but is not valid"""
    pass


class DMARCRecordNotFound(DMARCException):
    """Raised when a SPF record could not be found"""
    pass


class DMARCRecordInvalid(DMARCException):
    """Raised when a DMARC record is found but is not valid"""
    pass


class _SPFGrammar(Grammar):
    """Defines Pyleri grammar for SPF records"""
    version_tag = Regex("v=spf[\d.]+")
    mechanism = Regex("([?+-~]?)(mx|ip4|ip6|exists|include|all|a|redirect|exp|ptr)[:=]?([\w+\/_.:\-{%}]*)")
    START = Sequence(version_tag, Repeat(mechanism))


class _DMARCGrammar(Grammar):
    """Defines Pyleri grammar for DMARC records"""
    version_tag = Regex("v=DMARC[\d.]+;")
    tag = Regex("[a-z]{1,5}")
    equals = Token("=")
    value = Regex("[\w.:@/+!,_\-]+")
    tag_value = Sequence(tag, equals, value)
    START = Sequence(version_tag, List(tag_value, delimiter=";", opt=True))

dmarc_regex = compile(r"([a-z]{1,5})=([\w.:@/+!,_\-]+)")
spf_regex = compile(r"([?+-~]?)(mx|ip4|ip6|exists|include|all|a|redirect|exp|ptr)[:=]?([\w+\/_.:\-{%}]*)")


tag_values = dict(adkim=dict(name="DKIM Alignment Mode",
                             default="r",
                             description='In relaxed mode, the Organizational Domains of both the DKIM-'
                                         'authenticated signing domain (taken from the value of the "d=" tag in'
                                         'the signature) and that of the RFC5322.From domain must be equal if'
                                         'the identifiers are to be considered aligned.'),
                  aspf=dict(name="SPF alignment mode",
                            default="r",
                            description='In relaxed mode, the [SPF]-authenticated domain and RFC5322 From '
                                        'domain must have the same Organizational Domain. In strict mode,'
                                        'only an exact DNS domain match is considered to produce Identifier'
                                        'Alignment.'),
                  fo=dict(name="Failure Reporting Options",
                          default="0",
                          description='Provides requested options for generation of failure reports. '
                                      'Report generators MAY choose to adhere to the requested options. '
                                      'This tag\'s content MUST be ignored if a "ruf" tag (below) is not '
                                      'also specified. The value of this tag is a colon-separated list '
                                      'of characters that indicate failure reporting options.',
                          values={"0": 'Generate a DMARC failure report if all underlying '
                                       'authentication mechanisms fail to produce an aligned "pass" '
                                       'result.',
                                  "1": 'Generate a DMARC failure report if any underlying '
                                       'authentication mechanism produced something other than an '
                                       'aligned "pass" result.',
                                  "d": 'Generate a DKIM failure report if the message had a signature '
                                       'that failed evaluation, regardless of its alignment. DKIM-'
                                       'specific reporting is described in AFRF-DKIM.',
                                  "s": 'Generate an SPF failure report if the message failed SPF '
                                       'evaluation, regardless of its alignment. SPF-specific '
                                       'reporting is described in AFRF-SPF'
                                  }
                          ),
                  p=dict(name="Requested Mail Receiver Policy",
                         default="none",
                         description='Indicates the policy to be enacted by the Receiver at '
                                     'the request of the Domain Owner. Policy applies to the domain '
                                     'queried and to subdomains, unless subdomain policy is explicitly '
                                     'described using the "sp" tag.',
                         values={"none": 'The Domain Owner requests no specific action be taken '
                                         'regarding delivery of messages.',
                                 "quarantine": 'The Domain Owner wishes to have email that fails the '
                                               'DMARC mechanism check be treated by Mail Receivers as '
                                               'suspicious.  Depending on the capabilities of the Mail'
                                               'Receiver, this can mean "place into spam folder", "scrutinize '
                                               'with additional intensity", and/or "flag as suspicious".',
                                 "reject": 'The Domain Owner wishes for Mail Receivers to reject '
                                         'email that fails the DMARC mechanism check. Rejection SHOULD '
                                         'occur during the SMTP transaction.'
                                 }
                         ),
                  pct=dict(name="Percentage",
                           default=100,
                           description='Integer percentage of messages from the Domain Owner\'s '
                                       'mail stream to which the DMARC policy is to be applied. However, '
                                       'this MUST NOT be applied to the DMARC-generated reports, all of'
                                       'which must be sent and received unhindered.  The purpose of the'
                                       '"pct" tag is to allow Domain Owners to enact a slow rollout'
                                       'enforcement of the DMARC mechanism.'
                           ),
                  rf=dict(name="Report Format",
                          default="afrf",
                          description='A list seperated by colons of one or more report formats as'
                                      'requested by the Domain Owner to be used when a message fails both'
                                      'SPF and DKIM tests to report details of the individual failure. '
                                      'only "afrf" (the auth-failure report type) is currently supported'
                                      'in the DMARC standard.'
                          ),
                  ri=dict(name="Report Interval",
                          default=86400,
                          description='Indicates a request to Receivers to generate aggregate reports separated by no '
                                      'more than the requested number of seconds. DMARC implementations '
                                      'MUST be able to provide daily reports and SHOULD be able to '
                                      'provide hourly reports when requested. However, anything other '
                                      'than a daily report is understood to be accommodated on a best-effort basis.'
                          ),
                  rua=dict(name="Aggregate Feedback Addresses",
                           description=' A comma-separated list DMARC URIs to which aggregate feedback is to be sent.'
                           ),
                  ruf=dict(name="Forensic Feedback Addresses",
                           description=' A comma-separated list DMARC URIs to which forensic feedback is to be sent.'),
                  sp=dict(name="Subdomain Policy",
                          description='Indicates the policy to be enacted by the Receiver at '
                                      'the request of the Domain Owner. It applies only to subdomains of '
                                      'the domain queried and not to the domain itself. Its syntax is '
                                      'identical to that of the "p" tag defined above. If absent, the '
                                      'policy specified by the "p" tag MUST be applied for subdomains.'
                          ),
                  v=dict(name="Version",
                         default="DMARC1",
                         description='Identifies the record retrieved '
                                     'as a DMARC record. It MUST have the value of "DMARC1". The value'
                                     'of this tag MUST match precisely; if it does not or it is absent, '
                                     'the entire retrieved record MUST be ignored. It MUST be the first'
                                     'tag in the list.')
                  )

spf_qualifiers = {
    "": "pass",
    "?": "neutral",
    "+": "pass",
    "-": "fail",
    "~": "softfail"
}


def query_dmarc_record(domain, nameservers=None):
    """
    Queries DNS for a DMARC record
    Args:
        domain (str): A top-level domain (TLD)
        nameservers (list): A list of nameservers to query

    Returns:
        str: An unparsed DMARC string
    """
    target = "_dmarc.{0}".format(domain.lower().replace("_dmarc.", ""))
    try:
        resolver = dns.resolver.Resolver()
        if nameservers:
            resolver.nameservers = nameservers
        answer = resolver.query(target, "TXT")[0].to_text().strip('"')
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        raise DMARCRecordNotFound("A TXT record does not exist at {0}".format(target))
    except dns.exception.DNSException as error:
        raise DMARCRecordNotFound(error.msg)

    return answer


def get_dmarc_tag_description(tag, value=None):
    """
    Get the name, default value, and description for a DMARC tag, amd/or a description for a tag value
    
    Args:
        tag (str): A DMARC tag
        value (str): An optional value

    Returns:
        dict: A dictionary containing the tag's ``name``, ``default`` value, and a ``description`` of the tag or value  
    """
    name = tag_values[tag]["name"]
    description = tag_values[tag]["description"]
    default = None
    if "default" in tag_values[tag]:
        default = tag_values[tag]["default"]
    if value and "values" in tag_values[tag] and value in tag_values[tag]["values"][value]:
        description = tag_values[tag]["values"][value]

    return dict(name=name, default=default, description=description)


def parse_dmarc_record(record, include_tag_descriptions=False):
    """
    Parses a DMARC record
    
    Args:
        record (str): A DMARC record 
        include_tag_descriptions (bool): Include descriptions in parsed results 

    Returns:
        dict: The DMARC record parsed by key

    """
    record = record.strip('"')
    dmarc_syntax_checker = _DMARCGrammar()
    parsed_record = dmarc_syntax_checker.parse(record)
    if not parsed_record.is_valid:
        expecting = list(map(lambda x: unicode(x).strip('"'), list(parsed_record.expecting)))
        raise DMARCRecordInvalid("Error: Expected {0} at position {1} in: {2}".format(" or ".join(expecting),
                                                                                      parsed_record.pos, record))

    pairs = dmarc_regex.findall(record)
    tags = dict()

    # Find explicit tags
    for pair in pairs:
        tags[pair[0]] = dict(value=unicode(pair[1]), explicit=True)

    # Include implicit tags and their defaults
    for tag in tag_values.keys():
        if tag not in tags and "default" in tag_values[tag]:
            tags[tag] = dict(value=tag_values[tag]["default"], explicit=False)
        if "sp" not in tags:
            tags["sp"] = dict(value=tags["p"]["value"], explicit=False)

    # Validate tag values
    for tag in tags:
        if tag in tag_values and "values" in tag_values[tag] and tags[tag]["value"] not in tag_values[tag]["values"]:
            raise DMARCRecordInvalid("Tag {0} must have one of the following values: {1} - not {2}".format(
                tag,
                ",".join(tag_values[tag]["values"]),
                tags[tag]["value"]
            ))

    try:
        tags["pct"]["value"] = int(tags["pct"]["value"])
    except ValueError:
        raise DMARCRecordInvalid("The value of the pct tag must be an integer")

    try:
        tags["ri"]["value"] = int(tags["ri"]["value"])
    except ValueError:
        raise DMARCRecordInvalid("The value of the ri tag must be an integer")

    # Add descriptions if requested
    if include_tag_descriptions:
        for tag in tags:
            details = get_dmarc_tag_description(tag, tags[tag]["value"])
            tags[tag]["name"] = details["name"]
            if details["default"]:
                tags[tag]["default"] = details["default"]
            tags[tag]["description"] = details["description"]

    return tags


def get_dmarc_record(domain, include_tag_descriptions=False, nameservers=None):
    """
    Retrieves a DMARC record for a domain and parses it

    Args:
        domain (str): A top-level domain (TLD)
        include_tag_descriptions (bool): Include descriptions in parsed results
        nameservers (list): A list of nameservers to query

    Returns:
        dict: The DMARC record parsed by key

    """
    record = query_dmarc_record(domain, nameservers=nameservers)
    tags = parse_dmarc_record(record, include_tag_descriptions=include_tag_descriptions)

    return dict(record=record, tags=tags)


def query_spf_record(domain, nameservers=None):
    """
    Queries DNS for a SPF record
    Args:
        domain (str): A domain name
        nameservers (list): A list of nameservers to query

    Returns:
        str: An unparsed SPF string
    """
    try:
        resolver = dns.resolver.Resolver()
        if nameservers:
            resolver.nameservers = nameservers
        answer = resolver.query(domain, "TXT")
        spf_record = None
        for record in answer:
            record = record.to_text()
            if record.startswith('"v=spf1'):
                spf_record = record.replace(' "', '').replace('"', '')
                break
        if spf_record is None:
            raise SPFRecordNotFound("{0} does not have a SPF record".format(domain))
    except dns.resolver.NoAnswer:
        raise SPFRecordNotFound("{0} does not have a SPF record".format(domain))
    except dns.resolver.NXDOMAIN:
        raise SPFRecordNotFound("The domain {0} does not exist".format(domain))
    except dns.exception.DNSException as error:
        raise SPFRecordInvalid(error)

    return spf_record


def _get_mx_hosts(domain, nameservers=None):
    """
    Queries DNS for a list of Mail Exchange hosts 
    
    Args:
        domain (str): A domain name
        nameservers (list): A list of nameservers to query

    Returns:
        list: A list of Mail Exchange hosts

    """
    try:
        resolver = dns.resolver.Resolver()
        if nameservers:
            resolver.nameservers = nameservers
        answers = resolver.query(domain, "MX")
        hosts = list(map(lambda r: r.to_text().split(" ")[-1].rstrip("."), answers))
    except dns.resolver.NXDOMAIN:
        raise SPFRecordInvalid("The domain {0} does not exist".format(domain))
    except dns.resolver.NoAnswer:
        raise SPFRecordInvalid("{0} does not have any MX records".format(domain))
    except dns.exception.DNSException as error:
        raise SPFRecordInvalid(error)

    return hosts


def _get_a_records(domain, nameservers=None):
    """
    Queries DNS for A and AAAA records
    
    Args:
        domain (str): A domain name
        nameservers (list): A list of nameservers to query

    Returns:
        list: A list of IPv4 and IPv6 addresses

    """
    records = []
    try:
        resolver = dns.resolver.Resolver()
        if nameservers:
            resolver.nameservers = nameservers
        answers = resolver.query(domain, "A")
        records = list(map(lambda r: r.to_text().rstrip("."), answers))
        answers = resolver.query(domain, "AAAA")
        records += list(map(lambda r: r.to_text().rstrip("."), answers))
    except dns.resolver.NXDOMAIN:
        raise SPFRecordInvalid("The domain {0} does not exist".format(domain))
    except dns.resolver.NoAnswer:
        # Sometimes a domain will only have A or AAAA records, but not both, and that's ok
        pass
    except dns.exception.DNSException as error:
        raise SPFRecordInvalid(error)
    finally:
        if len(records) == 0:
            raise SPFRecordInvalid("{0} does not have any A or AAAA records".format(domain))

    return records


def _get_txt_records(domain, nameservers=None):
    """
    Queries DNS for TXT records

    Args:
        domain (str): A domain name
        nameservers (list): A list of nameservers to query

    Returns:
        list: A list of TXT records

    """
    try:
        resolver = dns.resolver.Resolver()
        if nameservers:
            resolver.nameservers = nameservers
        answers = resolver.query(domain, "TXT")
        records = list(map(lambda r: r.to_text().replace(' "', '').replace('"', ''), answers))
    except dns.resolver.NXDOMAIN:
        raise SPFRecordInvalid("The domain {0} does not exist".format(domain))
    except dns.resolver.NoAnswer:
        raise SPFRecordInvalid("The domain {0} does not have any TXT records".format(domain))
    except dns.exception.DNSException as error:
        raise SPFRecordInvalid(error)

    return records


def parse_spf_record(record, domain, nameservers=None):
    """
    Parses a SPF record, including resolving a, mx, and include mechanisms
    
    Args:
        record (str): An SPF record 
        domain (str): The domain that the SPF record came from
        nameservers (list): A list of nameservers to query

    Returns:
        dict: A dictionary containing a parsed SPF record and warinings 
    """
    record = record.replace(' "', '').replace('"', '')
    warnings = []
    spf_syntax_checker = _SPFGrammar()
    parsed_record = spf_syntax_checker.parse(record.lower())
    if not parsed_record.is_valid:
        expecting = list(map(lambda x: unicode(x).strip('"'), list(parsed_record.expecting)))
        raise SPFRecordInvalid("Error: Expected {0} at position {1} in: {2}".format(" or ".join(expecting),
                                                                                    parsed_record.pos, record))
    matches = spf_regex.findall(record.lower())
    results = {
        "pass": [],
        "neutral": [],
        "softfail": [],
        "fail": [],
        "include": dict(),
        "redirect": None,
        "exp": None,
        "all": "neutral"
    }

    for match in matches:
        result = spf_qualifiers[match[0]]
        mechanism = match[1]
        value = match[2]

        try:
            if mechanism == "a":
                if value == "":
                    a_records = _get_a_records(domain, nameservers=nameservers)
                else:
                    a_records = _get_a_records(value, nameservers=nameservers)
                for record in a_records:
                    results[result].append(dict(mechanism=mechanism, value=record))
            elif mechanism == "mx":
                if value == "":
                    mx_hosts = _get_mx_hosts(domain, nameservers=nameservers)
                else:
                    mx_hosts = _get_mx_hosts(value, nameservers=nameservers)
                for host in mx_hosts:
                    results[result].append(dict(mechanism=mechanism, value=host))
            elif mechanism == "redirect":
                results["redirect"] = value
            elif mechanism == "exp":
                results["exp"] = _get_txt_records(value)[0]
            elif mechanism == "all":
                results["all"] = result
            elif mechanism == "include":
                results["include"][value] = get_spf_record(value, nameservers=nameservers)
            elif mechanism == "ptr":
                warnings.append("ptr mechanism should not be used "
                                "https://tools.ietf.org/html/rfc7208#section-5.5")
            else:
                results[result].append(dict(mechanism=mechanism, value=value))
        except SPFException as warning:
            warnings.append(unicode(warning))

    return dict(results=results, warnings=warnings)


def get_spf_record(domain, nameservers=None):
    """
    Retrieves and parses an SPF record 
    
    Args:
        domain (str): A domain name
        nameservers (list): A list of nameservers to query

    Returns:
        dict: An SPF record parsed by result

    """
    record = query_spf_record(domain, nameservers=nameservers)
    record = parse_spf_record(record, domain, nameservers=nameservers)

    return record


def check_domains(domains, output_format="json", output_path=None, include_dmarc_tag_descriptions=False,
                  nameservers=None):
    """
    Check the given domains for SPF and DMARC records, parse them, and return them
    
    Args:
        domains (list): A list of domains to check 
        output_format (str): ``json`` or ``csv``
        output_path (str): Save output to the given file path 
        include_dmarc_tag_descriptions (bool): Include descriptions of DMARC tags and/or tag values in the results
        nameservers (list): A list of nameservers to query

    Returns:
        dict: Parsed SPF and DMARC records

    """
    output_format = output_format.lower()
    domains = sorted(list(set(map(lambda d: d.rstrip(".\r\n").lower(), domains))))
    if output_format not in ["json", "csv"]:
        raise ValueError("Invalid output format {0}. Valid options are json and csv.".format(output_format))
    if output_format == "csv":
        fields = ["domain", "spf_record", "dmarc_record", "spf_valid", "dmarc_valid", "spf_error", "spf_warnings",
                  "dmarc_error", "dmarc_adkim", "dmarc_aspf", "dmarc_fo", "dmarc_p", "dmarc_pct", "dmarc_rf", "dmarc_ri",
                  "dmarc_rua", "dmarc_ruf", "dmarc_sp"]
        sorted(list(set(map(lambda d: d.rstrip(".").rstrip(), domains))))
        if output_path:
            output_file = open(output_path, "w", newline="\n")
        else:
            output_file = StringIO()
        writer = DictWriter(output_file, fieldnames=fields)
        writer.writeheader()
        for domain in domains:
            row = dict(domain=domain, spf_valid=True, dmarc_valid=True)
            try:
                row["spf_record"] = query_spf_record(domain, nameservers=nameservers)
                row["spf_warnings"] = " ".join(parse_spf_record(row["spf_record"], row["domain"])["warnings"])
            except SPFException as error:
                row["spf_error"] = error
                row["spf_valid"] = False
            try:
                row["dmarc_record"] = query_dmarc_record(domain, nameservers=nameservers)
                dmarc = parse_dmarc_record(row["dmarc_record"])
                row["dmarc_adkim"] = dmarc["adkim"]["value"]
                row["dmarc_aspf"] = dmarc["aspf"]["value"]
                row["dmarc_fo"] = dmarc["fo"]["value"]
                row["dmarc_p"] = dmarc["p"]["value"]
                row["dmarc_pct"] = dmarc["pct"]["value"]
                row["dmarc_rf"] = dmarc["rf"]["value"]
                row["dmarc_ri"] = dmarc["ri"]["value"]
                row["dmarc_sp"] = dmarc["sp"]["value"]
                if "rua" in dmarc:
                    row["dmarc_rua"] = dmarc["rua"]["value"]
                if "ruf" in dmarc:
                    row["dmarc_ruf"] = dmarc["ruf"]["value"]
            except DMARCException as error:
                row["dmarc_error"] = error
                row["dmarc_valid"] = False
            writer.writerow(row)
            output_file.flush()
        if output_path is None:
            return output_file.getvalue()
    elif output_format == "json":
        results = []
        for domain in domains:
            domain_results = dict(domain=domain)
            domain_results["spf"] = dict(record=None, valid=True)
            try:
                domain_results["spf"]["record"] = query_spf_record(domain, nameservers=nameservers)
                parsed_spf = parse_spf_record(domain_results["spf"]["record"],
                                              domain_results["domain"],
                                              nameservers=nameservers)
                domain_results["spf"]["results"] = parsed_spf["results"]
                domain_results["spf"]["warnings"] = parsed_spf["warnings"]
            except SPFException as error:
                domain_results["spf"]["error"] = unicode(error)
                domain_results["spf"]["valid"] = False

            domain_results["dmarc"] = dict(record=None, valid=True)
            try:
                domain_results["dmarc"]["record"] = query_dmarc_record(domain, nameservers=nameservers)
                domain_results["dmarc"]["keys"] = parse_dmarc_record(domain_results["dmarc"]["record"],
                                                                     include_tag_descriptions=
                                                                     include_dmarc_tag_descriptions)
            except DMARCException as error:
                domain_results["dmarc"]["error"] = unicode(error)
                domain_results["dmarc"]["valid"] = False

            results.append(domain_results)
        if len(results) == 1:
            results = results[0]
        if output_path:
            with open(output_path, "w", newline="\n") as output_file:
                output_file.write(json.dumps(results, ensure_ascii=False, indent=2))

        return results


def _main():
    """Called when the module in executed"""
    arg_parser = ArgumentParser(description=__doc__)
    arg_parser.add_argument("domain", nargs="+",
                            help="One or ore domains, or single a path to a file containing a list of domains")
    arg_parser.add_argument("-f", "--format", default="json", help="Specify JSON or CSV output format")
    arg_parser.add_argument("-o", "--output", help="Output to a file path rather than printing to the screen")
    arg_parser.add_argument("-d", "--descriptions", action="store_true",
                            help="Include descriptions of DMARC tags in the JSON output")
    arg_parser.add_argument("-n", "--nameserver", nargs="+", help="Nameservers to query")
    arg_parser.add_argument("-v", "--version", action="version", version=__version__)
    args = arg_parser.parse_args()

    domains = args.domain
    if len(domains) == 1 and path.exists(domains[0]):
        with open(domains[0]) as domains_file:
            domains = list(map(lambda l: l.rstrip(".\r\n"), domains_file.readlines()))
    results = check_domains(domains, output_format=args.format, output_path=args.output,
                            nameservers=args.nameserver)

    if args.output is None:
        if args.format.lower() == "json":
            results = json.dumps(results, ensure_ascii=False, indent=2)

        print(results)

if __name__ == "__main__":
    _main()
