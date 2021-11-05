#!/usr/bin/env python3
__copyright__ = "Copyright (C) 2021 Dave Wallace"
__license__ = "GNU GPLv2"

"""Paycheck source.

Data format
===========

To use, first download paycheck PDF files into a directory on the
filesystem. This importer requires *.pdf paycheck files to be converted to the
mimetype:text/plain using 'less':

    less <paycheck>.pdf > <data dir>/YYYY-MM-DD-<paycheck>.txt

where the date prefix is specified by the paycheck date in the file contents.

You might have a data directory structure like:

      data/
        Financial-Records/
          Paychecks/
            Cisco_Systems_Inc/
              TY<year>/
                *.txt



Specifying the source to beancount_import
=========================================

Within your Python script for invoking beancount_import, you might use an
expression like the following to specify the Cisco Paycheck source:

    data_dir = os.environ['PTA_DATA']
    csco_paycheck_dir = os.path.join(data_dir, 'Financial-Records/Paychecks',
        f'Cisco_Systems_Inc/TY{tax_year}')

    data_sources = [
        dict(
            module = 'beancount_import.source.paycheck',
            paycheck_dir = csco_paycheck_dir
            ),
        ),
    ]



Imported transaction format
===========================

"""
import datetime
import fileinput
import os
import re
from dataclasses import dataclass, field
from enum import IntEnum, unique
from glob import glob
from io import StringIO
from pprint import pformat
# TODO: Clean up unused types
from typing import (AbstractSet, Callable, Dict, Iterable, Iterator, List,
                    Mapping, NamedTuple, Optional, Sequence, Set, Tuple, Type,
                    TypeVar, Union, cast)

# DAW DEBUG:
import debugpy
from beancount.core import amount, data
from beancount.core.amount import Amount
from beancount.core.data import (EMPTY_SET, Balance, Commodity, Directive, Open,
                                 Posting, Price, Transaction)
from beancount.core.flags import FLAG_OKAY
from beancount.core.number import MISSING, ZERO, D, Decimal
from beancount.core.position import CostSpec

from .. import amount_parsing
from ..journal_editor import JournalEditor
from ..matching import FIXME_ACCOUNT, SimpleInventory
from . import (ImportResult, InvalidSourceReference, LogFunction, Source,
               SourceResults)


@unique
class CheckFormat(IntEnum):
    """CSCO Check Format Enumeration."""

    UNKNOWN = 0
    CSCO_TY2020 = 1  # Cisco Systems TY2020 Text Format
    CSCO_TY2021 = 2  # Cisco Systems TY2021 Text Format


@dataclass
class TxtData:
    description: str
    data: List


@dataclass
class TxtFile:
    log_status: LogFunction
    filename: str
    company: str
    first_name: str
    last_name: str
    employee_id: str
    date: str = field(init=False)
    format: int = CheckFormat.UNKNOWN
    data_regex: str = "\d+\.\d{2}-*"
    data: List = field(default_factory=list)
    _contents: List = field(repr=False, default_factory=list)

    def __post_init__(self):
        self.date = os.path.basename(self.filename)[:10]
        self.get_contents()
        self.verify_format()
        self.verify_date()
        self.extract_data()

    def get_contents(self):
        if not self._contents:
            for line in fileinput.input(self.filename):
                # Remove currency symbols
                line = line.rstrip().replace(",", "").replace("$", "")
                # Remove non-data
                line = line.replace("  -  ", "     ")
                # Convert negative currency parens with minus sign prefix
                line = line.replace(")", "").replace("(", "-")
                # Fix single space separated data & description values
                line = re.sub(f"({self.data_regex}) (\w)", r"\1  \2", line)
                # Fix  single space separated description & values
                line = re.sub(f"(\w) ({self.data_regex})", r"\1  \2", line)
                self._contents.append(line)

    def find_in_contents(self, regex):
        for i, line in enumerate(self._contents):
            match = re.search(regex, line)
            if match:
                return [True, match]
        return [False, None]

    def verify_format(self):
        if (
            self.find_in_contents(self.company)[0]
            and self.find_in_contents(self.first_name)[0]
            and self.find_in_contents(self.last_name)[0]
            and self.find_in_contents(self.employee_id)[0]
        ):
            if re.search("Cisco", self.company):
                if re.search(
                    "E A R N I N G S      S T A T E M E N T", self._contents[0]
                ):
                    self.format = CheckFormat.CSCO_TY2021
                else:
                    self.format = CheckFormat.CSCO_TY2020
            else:
                raise ValueError(f"Unknown company: {self.company}")

    def verify_date(self):
        if self.format is CheckFormat.CSCO_TY2020:
            date_regex = "\s+(\d{2}/\d{2}/\d{4})"
        else:
            date_regex = "Check Date:\s+(\d{2}/\d{2}/\d{4})"

        found, match = self.find_in_contents(date_regex)
        if found:
            try:
                check_date = datetime.datetime.strptime(match.group(1), "%m/%d/%Y")
            except ValueError:
                raise ValueError(f"Date not found in filename: ({self.filename})")
            if self.date != f"{check_date.date()}":
                raise ValueError(f"Invalid date in filename: ({self.date})!")
        else:
            raise ValueError("Date not found in contents!")

    def extract_data(self):
        raw_data = []
        for line in self._contents:
            # Split the line into
            values = [x.lstrip() for x in list(filter(None, re.split(r"  ", line)))]
            if values and len(values) > 0:
                column = None
                description = None
                gather_data = False
                for i, token in enumerate(values):
                    # Convert negative numbers with trailing minus sign
                    if token.endswith("-"):
                        token = "-" + token.rstrip("-")
                    # Look for 'data'
                    found_data = re.search(self.data_regex, token)
                    if found_data is not None:
                        if not gather_data:
                            gather_data = True
                            column = TxtData(description.getvalue(), [])
                            description = None
                        column.data.append(Decimal(token))
                    # Stop gathering data,
                    #   - add column to list,
                    #   - start gathering description string
                    elif gather_data:
                        assert column
                        raw_data.append(column)
                        gather_data = False
                        column = None
                        description = StringIO()
                        description.write(token)
                    # Start gathering description string
                    elif description is None:
                        description = StringIO()
                        description.write(token)
                    # Continue gathering description string
                    else:
                        description.write(f" {token}")
                if gather_data:
                    raw_data.append(column)
        for txt_data in raw_data:
            if len(txt_data.data) >= 2:
                self.data.append(txt_data)

        self.log_status(f"------[ {self.filename} ]------")
        [self.log_status(pformat(x, indent=2)) for x in self.data]
        self.log_status("########################")


def valid_filename(filename):
    is_valid = True
    fname = os.path.basename(filename)
    suffix = fname.split(".")[-1]
    if not os.path.isfile(filename):
        print(f"ERROR: File not found: {filename}")
        is_valid = False
    elif suffix != "txt":
        print(f"ERROR: Invalid filename suffix '{suffix}': {fname}")
        is_valid = False
    elif len(fname) < 14:
        print(f"ERROR: Invalid filename length ({len(fname)}): {fname}")
        is_valid = False
    else:
        date = fname[:10]
        try:
            datetime.datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            print(f"ERROR: Invalid file name date prefix {date}: {fname}")
            is_valid = False
    return is_valid


RawTransactionEntry = NamedTuple(
    "RawTransactionEntry",
    [
        ("filename", str),
        ("date", datetime.date),
        ("fitid", str),
        ("trantype", str),
        ("total", Decimal),
        ("incometype", Optional[str]),
        ("inv401ksource", Optional[str]),
        ("memo", Optional[str]),
        ("name", Optional[str]),
        ("trntype", Optional[str]),
        ("uniqueid", Optional[str]),
        ("units", Optional[Decimal]),
        ("unitprice", Optional[Decimal]),
        ("tferaction", Optional[str]),
        ("fees", Optional[Decimal]),
        ("commission", Optional[Decimal]),
        ("checknum", Optional[str]),
    ],
)


class CscoPaycheckSource(Source):
    def __init__(
        self,
        paycheck_dir: str,
        company: str,
        first_name: str,
        last_name: str,
        employee_id: Optional[str],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.paycheck_files = [
            os.path.realpath(x) for x in glob(os.path.join(paycheck_dir, "*.txt"))
        ]
        self.company = company
        self.first_name = first_name
        self.last_name = last_name
        self.employee_id = employee_id
        self.parsed_files = []
        for filename in sorted(self.paycheck_files):
            if not valid_filename(filename):
                continue
            txtfile = TxtFile(
                self.log_status, filename, company, first_name, last_name, employee_id
            )
            if txtfile.format == CheckFormat.UNKNOWN:
                print(f"ERROR: Unknown file format for {filename}")
                continue
            self.log_status(pformat(txtfile, indent=2))
            self.parsed_files.append(txtfile)
        debugpy.breakpoint()

    def get_example_key_value_pairs(self, transaction: Transaction, posting: Posting):
        result = dict()
        # def maybe_add_key(key):
        #     x = posting.meta.get(key)
        #     if x is not None:
        #         result[key] = x
        # maybe_add_key(VENMO_TYPE_KEY)
        # if VENMO_TRANSFER_KEY in posting.meta:
        #     maybe_add_key(VENMO_ACCOUNT_DESCRIPTION_KEY)
        # if VENMO_PAYMENT_KEY in posting.meta:
        #     maybe_add_key(VENMO_DESCRIPTION_KEY)
        #     maybe_add_key(VENMO_PAYEE_KEY)
        #     maybe_add_key(VENMO_PAYER_KEY)
        return result

    def is_posting_cleared(self, posting: Posting):
        if posting.meta is None:
            return False
        # return VENMO_TRANSFER_KEY in posting.meta or VENMO_PAYMENT_KEY in posting.meta

    def prepare(self, journal, results: SourceResults):
        pass

    @property
    def name(self):
        return "cscopaychecks"


def load(spec, log_status):
    return CscoPaycheckSource(log_status=log_status, **spec)
