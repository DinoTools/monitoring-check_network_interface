#!/usr/bin/env python3
# SPDX-FileCopyrightText: PhiBo DinoTools (2021)
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
from datetime import datetime
import logging
import re
from typing import Any, Dict, Optional

import nagiosplugin
import psutil

logger = logging.getLogger('nagiosplugin')


class MissingValue(ValueError):
    pass


class BooleanContext(nagiosplugin.Context):
    def performance(self, metric, resource):
        return nagiosplugin.performance.Performance(
            label=metric.name,
            value=1 if metric.value else 0
        )


class NetworkResource(nagiosplugin.Resource):
    name = "NET"

    def __init__(self, if_name: str):
        super().__init__()

        self.if_name = if_name

    @staticmethod
    def _calc_rate(
            cookie: nagiosplugin.Cookie,
            name: str,
            cur_value: int,
            elapsed_seconds: float,
            factor: int
    ) -> float:
        old_value: Optional[int] = cookie.get(name)
        cookie[name] = cur_value
        if old_value is None:
            raise MissingValue(f"Unable to find old value for '{name}'")
        if elapsed_seconds is None:
            raise MissingValue("Unable to get elapsed seconds")
        return (cur_value - old_value) / elapsed_seconds * factor

    def probe(self):
        cur_time = datetime.now()
        ifs_stats: Dict[str, Any] = psutil.net_if_stats()
        ifs_counters: Dict[str, Any] = psutil.net_io_counters(pernic=True)

        if_stats = ifs_stats[self.if_name]
        if_counters = ifs_counters[self.if_name]

        logger.debug(f"{self.if_name} reported speed: {if_stats.speed}")
        logger.debug(f"{self.if_name} reported mtu: {if_stats.mtu}")

        yield nagiosplugin.Metric(
            name=f"{self.if_name}.status",
            value=if_stats.isup,
        )
        yield nagiosplugin.Metric(
            name=f"{self.if_name}.speed",
            value=if_stats.speed,
        )
        yield nagiosplugin.Metric(
            name=f"{self.if_name}.mtu",
            value=if_stats.mtu,
        )
        value_name_mappings = {
            "bytes_sent": None,
            "bytes_recv": None,
            "packets_sent": None,
            "packets_recv": None,
            "errors_in": "errin",
            "errors_out": "errout",
            "drops_in": "dropin",
            "drops_out": "dropout",
        }
        value_uom_mappings = {
            "bytes_sent": "B",
            "bytes_recv": "B",
            "packets_sent": "c",
            "packets_recv": "c",
            "errors_in": "c",
            "errors_out": "c",
            "drops_in": "c",
            "drops_out": "c",
        }
        value_factor_mappings = {}
        value_min_mappings = {}
        value_max_mappings = {
            "bytes_sent_rate": if_stats.speed * 1000 * 1000 / 8,
            "bytes_recv_rate": if_stats.speed * 1000 * 1000 / 8,
        }
        values = {}
        for value_name, attr_name in value_name_mappings.items():
            if attr_name is None:
                attr_name = value_name
            values[value_name] = getattr(if_counters, attr_name)
            yield nagiosplugin.Metric(
                name=f"{self.if_name}.{value_name}",
                value=values[value_name],
                uom=value_uom_mappings.get(value_name),
            )
        with nagiosplugin.Cookie(f"/tmp/check_network_interface_{self.if_name}.data") as cookie:
            last_time_tuple = cookie.get("last_time")
            elapsed_seconds = None
            if isinstance(last_time_tuple, (list, tuple)):
                last_time = datetime(*last_time_tuple[0:6])
                delta_time = cur_time - last_time
                elapsed_seconds = delta_time.total_seconds()

            for value_name in value_name_mappings.keys():
                try:
                    value = self._calc_rate(
                        cookie=cookie,
                        name=value_name,
                        cur_value=values[value_name],
                        elapsed_seconds=elapsed_seconds,
                        factor=value_factor_mappings.get(f"{value_name}_rate", 1)
                    )
                    yield nagiosplugin.Metric(
                        name=f"{self.if_name}.{value_name}_rate",
                        value=value,
                        uom=value_uom_mappings.get(f"{value_name}_rate"),
                        min=value_min_mappings.get(f"{value_name}_rate", 0),
                        max=value_max_mappings.get(f"{value_name}_rate"),
                    )
                except MissingValue as e:
                    logger.debug(f"{e}", exc_info=e)
            cookie["last_time"] = cur_time.timetuple()


class InterfaceStatusContext(BooleanContext):
    def evaluate(self, metric, resource: NetworkResource):
        if metric.value is True:
            return self.result_cls(nagiosplugin.state.Ok)
        elif metric.value is False:
            return self.result_cls(nagiosplugin.state.Critical, f"Interface {resource.if_name} is down", metric)
        return self.result_cls(nagiosplugin.state.Unknown,  f"Interface {resource.if_name} is unknown")


def main():
    argp = argparse.ArgumentParser(description=__doc__)
    argp.add_argument(
        "-i",
        "--interface",
        dest="interfaces",
        required=True,
        help="Name of the interface",
        metavar="NAME",
        action="append",
    )

    argp.add_argument(
        "--regex",
        default=False,
        help="Treat interface names as regex pattern",
        action="store_true",
    )

    argument_mappings = {
        "bytes_sent": {
            "fmt_metric": "{name} is {value} Bit/s",
        },
        "bytes_recv": {
            "fmt_metric": "{name} is {value} Bit/s",
        },
        "packets_sent": {},
        "packets_recv": {},
        "errors_in": {},
        "errors_out": {},
        "drops_in": {},
        "drops_out": {},
        "bytes_sent_rate": {
            "fmt_metric": "{name} is {value} Bit/s",
        },
        "bytes_recv_rate": {
            "fmt_metric": "{name} is {value} Bit/s",
        },
        "packets_sent_rate": {},
        "packets_recv_rate": {},
        "errors_in_rate": {},
        "errors_out_rate": {},
        "drops_in_rate": {},
        "drops_out_rate": {},
    }

    for argument_name, argument_config in argument_mappings.items():
        for argument_type in ("warning", "critical"):
            argp.add_argument(
                f"--{argument_type}-{argument_name.replace('_', '-')}",
                dest=f"{argument_type}_{argument_name}",
                help=argument_config.get("help"),
                default=argument_config.get("default"),
            )

    argp.add_argument('-v', '--verbose', action='count', default=0)
    args = argp.parse_args()

    interface_names = set()
    available_interface_names = psutil.net_if_stats().keys()
    logger.debug(f"Available interfaces: {', '.join(available_interface_names)}")
    logger.debug(f"Interface patterns/names: {', '.join(args.interfaces)}")
    logger.debug("Regex: {status}".format(status="enabled" if args.regex else "disabled"))
    for interface_name_pattern in args.interfaces:
        if args.regex:
            regex = re.compile(interface_name_pattern)
            for interface_name in available_interface_names:
                if regex.match(interface_name):
                    interface_names.add(interface_name)
        else:
            for interface_name in available_interface_names:
                if interface_name == interface_name_pattern:
                    interface_names.add(interface_name)

    logger.debug(f"Matching interfaces: {' '.join(interface_names)}")

    check = nagiosplugin.Check()
    for if_name in interface_names:
        check.add(NetworkResource(
            if_name=if_name
        ))

        check.add(InterfaceStatusContext(f"{if_name}.status"))
        check.add(nagiosplugin.ScalarContext(f"{if_name}.speed"))
        check.add(nagiosplugin.ScalarContext(f"{if_name}.mtu"))

        for argument_name, argument_config in argument_mappings.items():
            extra_kwargs = {}
            if "fmt_metric" in argument_config:
                extra_kwargs["fmt_metric"] = argument_config["fmt_metric"]

            context_class = argument_config.get("class", nagiosplugin.ScalarContext)
            check.add(context_class(
                name=f"{if_name}.{argument_name}",
                warning=getattr(args, f"warning_{argument_name}"),
                critical=getattr(args, f"critical_{argument_name}"),
                **extra_kwargs
            ))

    check.main(verbose=args.verbose)


if __name__ == "__main__":
    main()
