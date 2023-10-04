# -*- coding: utf-8 -*-
# @author: Tomas Vitvar, https://vitvar.com, tomas.vitvar@oracle.com

import click
from datetime import datetime, timedelta
from tqdm import tqdm
import os
import re
import json
import time
import threading
import sys
import subprocess

from ..log import SOALogReader, LogReader, OutLogEntry, get_files, list_files, DEFAULT_DATETIME_FORMAT, Index

from ..json2table import Table
from ..config import DATA_DIR

from .click_ext import BaseCommandConfig

INDEX_FILE = os.path.join(DATA_DIR, "wlsanalytics.index")


class DateTimeOption(click.Option):
    def type_cast_value(self, ctx, value):
        if value is None:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                time_value = datetime.strptime(value, "%H:%M:%S").time()
                today = datetime.now().date()
                return datetime.combine(today, time_value)
            except ValueError:
                try:
                    time_value = datetime.strptime(value, "%H:%M").time()
                    today = datetime.now().date()
                    return datetime.combine(today, time_value)
                except ValueError:
                    pass

        raise click.BadParameter("use values in the format '%Y-%m-%d %H:%M:%S', '%H:%M:%S' or '%H:%M'.")


class OffsetOption(click.Option):
    def type_cast_value(self, ctx, value):
        if value is None:
            return None
        offset_units = {"h": "hours", "d": "days", "m": "minutes"}
        if value[-1] in offset_units:
            try:
                offset_value = int(value[:-1])
                offset_unit = offset_units[value[-1]]
                return timedelta(**{offset_unit: offset_value})
            except (ValueError, KeyError):
                pass

        raise click.BadParameter("use values like '1h', '2d', '10m'.")


# def soaerrors_label_parser():
#     return [
#         {"pattern": "ErrMsg=([A-Z_0-9]+)", "label": lambda x: x.group(1)},  # BRM error
#         {"pattern": "(SBL-DAT-[0-9]+)", "label": lambda x: x.group(1)},  # Siebel data error
#         {"pattern": "(SBL-EAI-[0-9]+)", "label": lambda x: x.group(1)},  # Siebel EAI error
#         {
#             "pattern": "Response:\s+'?([0-9]+).*for url.+'http(.+)'",
#             "label": lambda x: x.group(1) + "_" + x.group(2).split("/")[-1],  # status code + service name
#         },
#     ]


@click.command(cls=BaseCommandConfig, log_handlers=["file"])
@click.argument("set", required=True)
def range(config, log, set):
    logs_set = config(f"sets.{set}")
    if logs_set is None:
        raise Exception(f"The log set '{set}' not found in the configuration file.")

    range_data = []
    for server_name, files in list_files(
        logs_set.directories, lambda fname: re.search(logs_set.filename_pattern, fname)
    ).items():
        range_item = {"server": server_name, "min": None, "max": None, "files": len(files), "size": 0}
        range_data.append(range_item)
        for fname in files:
            range_item["size"] += os.path.getsize(fname)
            reader = LogReader(fname, datetime_format=DEFAULT_DATETIME_FORMAT, logentry_class=OutLogEntry)
            first, _ = reader.get_datetime(True)
            last, _ = reader.get_datetime(False)
            if range_item["min"] is None or first < range_item["min"]:
                range_item["min"] = first
            if range_item["max"] is None or last > range_item["max"]:
                range_item["max"] = last

    range_data = sorted(range_data, key=lambda x: x["server"])
    table_def = [
        {"name": "SERVER", "value": "{server}", "help": "Server name"},
        {"name": "FILES", "value": "{files}", "help": "Number of files"},
        {
            "name": "SIZE [GB]",
            "value": "{size}",
            "format": lambda _, v, y: round(v / 1024 / 1024 / 1024, 2),
            "help": "Total size",
        },
        {
            "name": "MIN",
            "value": "{min}",
            "format": lambda _, v, y: v.replace(microsecond=0),
            "help": "Minimum datetime",
        },
        {
            "name": "MAX",
            "value": "{max}",
            "format": lambda _, v, y: v.replace(microsecond=0),
            "help": "Maximum datetime",
        },
    ]
    Table(table_def, None, False).display(range_data)


def make_label_function(label):
    def _x(m):
        try:
            return label.format(*list([""] + list(m.groups())))
        except Exception as e:
            return "__internal_error__"

    def label_function(m):
        return _x(m)

    return label_function


def load_parser(parsers_def, sets: list):
    _parser = []
    for parser_def in parsers_def:
        if any(item in parser_def["sets"] for item in sets):
            for rule in parser_def["rules"]:
                _parser.append({"pattern": rule["pattern"], "label": make_label_function(rule["label"])})
    return _parser


@click.command(cls=BaseCommandConfig, log_handlers=["file"])
@click.argument("set_name", required=True)
@click.option("--from", "-f", "time_from", cls=DateTimeOption, help="Start time (default: derived from --offset)")
@click.option("--to", "-t", "time_to", cls=DateTimeOption, help="End time (default: current time)")
@click.option("--offset", "-o", cls=OffsetOption, help="Time offset to derive --from from --to")
def errors(config, log, set_name, time_from, time_to, offset):
    logs_set = config(f"sets.{set_name}")
    if logs_set is None:
        raise Exception(f"The log set '{set_name}' not found in the configuration file.")

    if time_from is None and time_to is None:
        raise Exception("Either --from or --to must be specified.")

    if time_to is None:
        time_to = datetime.now()

    if time_from is None and offset is not None:
        time_from = time_to - offset

    start_time = time.time()
    print(f"-- Time range: {time_from} - {time_to}")
    print(f"-- Searching files in the set '{set_name}'")

    soa_files = get_files(
        logs_set.directories,
        time_from,
        time_to,
        lambda fname: re.search(logs_set.filename_pattern, fname),
    )

    if len(soa_files) == 0:
        print("-- No files found.")
        return

    total_size = sum([item["end_pos"] - item["start_pos"] for items in soa_files.values() for item in items])
    num_files = sum([len(items) for items in soa_files.values()])
    pbar = tqdm(
        desc=f"-- Reading entries from {num_files} files",
        total=total_size,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        ncols=100,
    )

    label_parser = load_parser(config("parsers"), [set_name])
    index = Index()

    def _read_entries(server, item):
        reader = SOALogReader(item["file"])
        reader.open()
        try:
            for entry in reader.read_errors(
                start_pos=item["start_pos"], time_to=time_to, progress=pbar, label_parser=label_parser, index=index
            ):
                d = entry.to_dict()
                d["file"] = item["file"]
                item["data"].append(d)
                item["data"][-1]["server"] = server
        finally:
            reader.close()

    for server, items in soa_files.items():
        for item in items:
            _read_entries(server, item)

    index.write(INDEX_FILE)

    pbar.close()
    data = []
    for server, items in soa_files.items():
        for item in items:
            data.extend(item["data"])

    data = sorted(data, key=lambda x: x["time"])

    if len(data) == 0:
        print("-- No errors found.")
        return

    print(f"-- Completed in {time.time() - start_time:.2f}s")

    table_def = [
        {"name": "TIME", "value": "{time}", "help": "Error time"},
        {"name": "SERVER", "value": "{server}", "help": "Server name"},
        {"name": "FLOW_ID", "value": "{flow_id}", "help": "Flow ID"},
        {"name": "COMPOSITE", "value": "{composite}", "help": "Composite name"},
        {"name": "VERSION", "value": "{version}", "help": "Composite version"},
        {"name": "LABEL", "value": "{label}", "help": "Error label"},
        {"name": "INDEX", "value": "{index}", "help": "Entry index"},
    ]
    Table(table_def, None, False).display(data)
    print(f"-- Errors: {len(data)}")


@click.command(cls=BaseCommandConfig, log_handlers=["file"])
@click.argument("id", required=True)
@click.option("--stdout", "-s", is_flag=True, help="Print to stdout instead of using less")
def index(config, log, id, stdout):
    index = Index(INDEX_FILE)
    filename, item = index.search(id)
    if filename is None:
        print(f"Index entry '{id}' not found.")
        return
    else:
        output = f"log_file: {filename}\n" + f"index_file: {INDEX_FILE}\n\n" + "\n".join(item["messages"])
        if not stdout:
            cmd = ["less"]
            subprocess.run(cmd, input=output.encode("utf-8"))
        else:
            print(output)


@click.group(help="Log commands.")
def log():
    pass


@click.group(help="SOA Log commands.")
def soa():
    pass


log.add_command(soa)
soa.add_command(errors)
soa.add_command(range)
soa.add_command(index)
