#!/usr/bin/env python
# Copyright 2015-2016 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import functools
import itertools
import logging
import sys

import chronos
from marathon.exceptions import MarathonError

from paasta_tools import __version__
from paasta_tools.autoscaling.autoscaling_cluster_lib import AutoscalingInfo
from paasta_tools.autoscaling.autoscaling_cluster_lib import get_autoscaling_info_for_all_resources
from paasta_tools.chronos_tools import get_chronos_client
from paasta_tools.chronos_tools import load_chronos_config
from paasta_tools.marathon_tools import get_marathon_clients
from paasta_tools.marathon_tools import get_marathon_servers
from paasta_tools.mesos.exceptions import MasterNotAvailableException
from paasta_tools.mesos_tools import get_mesos_master
from paasta_tools.metrics import metastatus_lib
from paasta_tools.utils import format_table
from paasta_tools.utils import load_system_paasta_config
from paasta_tools.utils import paasta_print
from paasta_tools.utils import PaastaColors
from paasta_tools.utils import print_with_indent


logging.basicConfig()
# kazoo can be really noisy - turn it down
logging.getLogger("kazoo").setLevel(logging.CRITICAL)
logging.getLogger("paasta_tools.autoscaling.autoscaling_cluster_lib").setLevel(logging.ERROR)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='',
    )
    parser.add_argument(
        '-g',
        '--groupings',
        nargs='+',
        default=['region'],
        help=(
            'Group resource information of slaves grouped by attribute.'
            'Note: This is only effective with -vv'
        ),
    )
    parser.add_argument('-t', '--threshold', type=int, default=90)
    parser.add_argument('--use-mesos-cache', action='store_true', default=False)
    parser.add_argument(
        '-a', '--autoscaling-info', action='store_true', default=False,
        dest="autoscaling_info",
    )
    parser.add_argument(
        '-v', '--verbose', action='count', dest="verbose", default=0,
        help="Print out more output regarding the state of the cluster",
    )
    parser.add_argument(
        '-H', '--humanize', action='store_true', dest="humanize", default=False,
        help="Print human-readable sizes",
    )
    return parser.parse_args(argv)


def get_marathon_framework_ids(marathon_clients):
    return [client.get_info().framework_id for client in marathon_clients]


def _run_mesos_checks(mesos_master, mesos_state, marathon_clients):
    try:
        marathon_framework_ids = get_marathon_framework_ids(marathon_clients)
    except (MarathonError, ValueError) as e:
        paasta_print(PaastaColors.red("CRITICAL: Unable to contact Marathon cluster: {}".format(e)))
        sys.exit(2)

    mesos_state_status = metastatus_lib.get_mesos_state_status(
        mesos_state=mesos_state,
        marathon_framework_ids=marathon_framework_ids,
    )

    metrics = mesos_master.metrics_snapshot()
    mesos_metrics_status = metastatus_lib.get_mesos_resource_utilization_health(
        mesos_metrics=metrics,
        mesos_state=mesos_state,
    )
    return mesos_state_status + mesos_metrics_status


def _run_marathon_checks(marathon_clients):
    try:
        marathon_results = metastatus_lib.get_marathon_status(marathon_clients)
        return marathon_results
    except (MarathonError, ValueError) as e:
        paasta_print(PaastaColors.red("CRITICAL: Unable to contact Marathon cluster: {}".format(e)))
        sys.exit(2)


def all_marathon_clients(marathon_clients):
    return [c for c in itertools.chain(marathon_clients.current, marathon_clients.previous)]


def main(argv=None):
    chronos_config = None
    args = parse_args(argv)

    system_paasta_config = load_system_paasta_config()

    master_kwargs = {}
    # we don't want to be passing False to not override a possible True
    # value from system config
    if args.use_mesos_cache:
        master_kwargs['use_mesos_cache'] = True
    master = get_mesos_master(**master_kwargs)

    marathon_servers = get_marathon_servers(system_paasta_config)
    marathon_clients = all_marathon_clients(get_marathon_clients(marathon_servers))

    try:
        mesos_state = master.state
        all_mesos_results = _run_mesos_checks(
            mesos_master=master,
            mesos_state=mesos_state,
            marathon_clients=marathon_clients,
        )
    except MasterNotAvailableException as e:
        # if we can't connect to master at all,
        # then bomb out early
        paasta_print(PaastaColors.red("CRITICAL:  %s" % e.message))
        sys.exit(2)

    # Check to see if Chronos should be running here by checking for config
    chronos_config = load_chronos_config()

    if chronos_config:
        chronos_client = get_chronos_client(chronos_config, cached=True)
        try:
            chronos_results = metastatus_lib.get_chronos_status(chronos_client)
        except (chronos.ChronosAPIError) as e:
            paasta_print(PaastaColors.red("CRITICAL: Unable to contact Chronos! Error: %s" % e))
            sys.exit(2)
    else:
        chronos_results = [metastatus_lib.HealthCheckResult(
            message='Chronos is not configured to run here',
            healthy=True,
        )]

    marathon_results = _run_marathon_checks(marathon_clients)

    mesos_ok = all(metastatus_lib.status_for_results(all_mesos_results))
    marathon_ok = all(metastatus_lib.status_for_results(marathon_results))
    chronos_ok = all(metastatus_lib.status_for_results(chronos_results))

    mesos_summary = metastatus_lib.generate_summary_for_check("Mesos", mesos_ok)
    marathon_summary = metastatus_lib.generate_summary_for_check("Marathon", marathon_ok)
    chronos_summary = metastatus_lib.generate_summary_for_check("Chronos", chronos_ok)

    healthy_exit = True if all([mesos_ok, marathon_ok, chronos_ok]) else False

    paasta_print("Master paasta_tools version: {}".format(__version__))
    metastatus_lib.print_results_for_healthchecks(mesos_summary, mesos_ok, all_mesos_results, args.verbose)
    if args.verbose > 1:
        for grouping in args.groupings:
            print_with_indent('Resources Grouped by %s' % grouping, 2)
            grouping_function = metastatus_lib.key_func_for_attribute(grouping)
            resource_info_dict = metastatus_lib.get_resource_utilization_by_grouping(
                grouping_function,
                mesos_state,
            )
            all_rows = [[
                grouping.capitalize(), 'CPU (used/total)', 'RAM (used/total)', 'Disk (used/total)',
                'GPU (used/total)', 'Agent count',
            ]]
            table_rows = []
            for attribute_value, resource_info_dict in resource_info_dict.items():
                resource_utilizations = metastatus_lib.resource_utillizations_from_resource_info(
                    total=resource_info_dict['total'],
                    free=resource_info_dict['free'],
                )
                healthcheck_utilization_pairs = [
                    metastatus_lib.healthcheck_result_resource_utilization_pair_for_resource_utilization(
                        utilization,
                        args.threshold,
                    )
                    for utilization in resource_utilizations
                ]
                healthy_exit = all(pair[0].healthy for pair in healthcheck_utilization_pairs)
                table_rows.append(metastatus_lib.get_table_rows_for_resource_info_dict(
                    attribute_value,
                    healthcheck_utilization_pairs,
                    args.humanize,
                ) + [str(resource_info_dict['slave_count'])])
            table_rows = sorted(table_rows, key=lambda x: x[0])
            all_rows.extend(table_rows)
            for line in format_table(all_rows):
                print_with_indent(line, 4)

        if args.autoscaling_info:
            print_with_indent("Autoscaling resources:", 2)
            headers = [field.replace("_", " ").capitalize() for field in AutoscalingInfo._fields]
            table = functools.reduce(
                lambda x, y: x + [(y)],
                get_autoscaling_info_for_all_resources(),
                [headers],
            )

            for line in format_table(table):
                print_with_indent(line, 4)

        if args.verbose >= 3:
            print_with_indent('Per Slave Utilization', 2)
            slave_resource_dict = metastatus_lib.get_resource_utilization_by_grouping(
                lambda slave: slave['hostname'],
                mesos_state,
            )
            all_rows = [['Hostname', 'CPU (used/total)', 'RAM (used//total)', 'Disk (used//total)', 'GPU (used/total)']]

            # print info about slaves here. Note that we don't make modifications to
            # the healthy_exit variable here, because we don't care about a single slave
            # having high usage.
            for attribute_value, resource_info_dict in slave_resource_dict.items():
                table_rows = []
                resource_utilizations = metastatus_lib.resource_utillizations_from_resource_info(
                    total=resource_info_dict['total'],
                    free=resource_info_dict['free'],
                )
                healthcheck_utilization_pairs = [
                    metastatus_lib.healthcheck_result_resource_utilization_pair_for_resource_utilization(
                        utilization,
                        args.threshold,
                    )
                    for utilization in resource_utilizations
                ]
                table_rows.append(metastatus_lib.get_table_rows_for_resource_info_dict(
                    attribute_value,
                    healthcheck_utilization_pairs,
                    args.humanize,
                ))
                table_rows = sorted(table_rows, key=lambda x: x[0])
                all_rows.extend(table_rows)
            for line in format_table(all_rows):
                print_with_indent(line, 4)
    metastatus_lib.print_results_for_healthchecks(marathon_summary, marathon_ok, marathon_results, args.verbose)
    metastatus_lib.print_results_for_healthchecks(chronos_summary, chronos_ok, chronos_results, args.verbose)

    if not healthy_exit:
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == '__main__':
    main()
