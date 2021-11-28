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

TBD. Add sanitized examples here

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
from typing import (
    AbstractSet,
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

# DAW DEBUG:
import debugpy
from beancount.core import amount, data
from beancount.core.amount import Amount
from beancount.core.data import (
    EMPTY_SET,
    Balance,
    Commodity,
    Directive,
    Open,
    Posting,
    Price,
    Transaction,
)
from beancount.core.flags import FLAG_OKAY
from beancount.core.number import MISSING, ZERO, Decimal
from beancount.core.position import CostSpec

from .. import amount_parsing
from ..journal_editor import JournalEditor
from ..matching import FIXME_ACCOUNT, SimpleInventory
from . import ImportResult, InvalidSourceReference, LogFunction, Source, SourceResults

PAYCHECK_KEY_FILE = "paycheck_file"
PAYCHECK_KEY_COMPANY = "paycheck_company"
PAYCHECK_KEY_EMPLOYEE = "paycheck_employee"


@unique
class PaycheckFormat(IntEnum):
    """Paycheck Format Enumeration."""

    UNKNOWN = 0
    CSCO_TY2020 = 1  # Cisco Systems TY2020 Text Format
    CSCO_TY2021 = 2  # Cisco Systems TY2021 Text Format


@unique
class ImportedDataType(IntEnum):
    """Data Import Type Enumeration."""

    UNKNOWN = 0
    INCOME = 1
    EXPENSE = 2
    DEPOSIT = 3


@dataclass
class PaycheckRule:
    meta_key: str
    import_type: ImportedDataType
    format: PaycheckFormat
    regex: str


paycheck_rule_map = [
    PaycheckRule(
        "paycheck_salary_csco_ty2020",
        ImportedDataType.INCOME,
        PaycheckFormat.CSCO_TY2020,
        "Salary - Exempt\s+-\s+-\s+(\d+,*\d+\.\d{2})\s+(\d+,*\d+\.\d{2})",
    ),
    PaycheckRule(
        "paycheck_salary_csco_ty2021",
        ImportedDataType.INCOME,
        PaycheckFormat.CSCO_TY2021,
        "Regular Salary\s+\d+.\d{2}\s+(\d+,*\d+\.\d{2})\s+(\d+,*\d+\.\d{2})",
    ),
]


@dataclass
class ImportAccountRule:
    account_name: str
    import_type: ImportedDataType
    format: PaycheckFormat
    currency: str
    data_regex: str
    filename: str
    lineno: int


@dataclass
class ImportedData:
    account_name: str
    import_type: ImportedDataType
    format: PaycheckFormat
    company: str
    employee: str
    date: datetime.date
    description: str
    memo: str
    units: Amount
    balance: Amount
    filename: str
    lineno: int

    def posting(self):
        meta = dict()
        meta[PAYCHECK_KEY_FILE] = self.filename
        if self.company is not None:
            meta[PAYCHECK_KEY_COMPANY] = self.company
        if self.employee is not None:
            meta[PAYCHECK_KEY_EMPLOYEE] = self.employee
        return Posting(
            account=self.account_name,
            units=self.units,
            cost=None,
            price=None,
            flag=None,
            meta=meta,
        )


@dataclass
class TxtData:
    description: str
    data: List
    filename: str
    lineno: int


@dataclass
class TxtFile:
    log_status: LogFunction
    filename: str
    company: str
    first_name: str
    last_name: str
    employee_id: str
    date: datetime.date = field(init=False)
    format: int = PaycheckFormat.UNKNOWN
    data_regex: str = "\d+\.\d{2}-*"
    data: List = field(default_factory=list)
    _contents: List = field(repr=False, default_factory=list)

    def __post_init__(self):
        self.date = datetime.datetime.strptime(
            os.path.basename(self.filename)[:10], "%Y-%m-%d"
        )
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
        for lineno, line in enumerate(self._contents, 1):
            match = re.search(regex, line)
            if match:
                return [True, match, lineno]
        return [False, None, None]

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
                    self.format = PaycheckFormat.CSCO_TY2021
                else:
                    self.format = PaycheckFormat.CSCO_TY2020
            else:
                raise ValueError(f"Unknown company: {self.company}")

    def verify_date(self):
        if self.format is PaycheckFormat.CSCO_TY2020:
            date_regex = "\s+(\d{2}/\d{2}/\d{4})"
        else:
            date_regex = "Check Date:\s+(\d{2}/\d{2}/\d{4})"

        found, match, line = self.find_in_contents(date_regex)
        if found:
            try:
                check_date = datetime.datetime.strptime(match.group(1), "%m/%d/%Y")
            except ValueError:
                raise ValueError(f"Date not found in filename: ({self.filename})")
            if self.date != check_date:
                raise ValueError(f"Invalid date in filename: ({self.date.date()})!")
        else:
            raise ValueError("Date not found in contents!")

    def extract_data(self):
        raw_data = []
        lineno = 0
        for line in self._contents:
            lineno += 1
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
                            column = TxtData(
                                description.getvalue(), [], self.filename, lineno
                            )
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
                self.data.append(dict(line=lineno, data=txt_data))

        self.log_status(f"------[ {self.filename} ]------")
        [self.log_status(pformat(x, indent=2)) for x in self.data]
        self.log_status("########################")

    def apply_account_rule(self, rule):
        if rule.import_type == ImportedDataType.INCOME:
            found, match, lineno = self.find_in_contents(rule.data_regex)
            if not found:
                return None
            current_income = Decimal(match.group(1))
            ytd_income = Decimal(match.group(2))
            if "Salary" in rule.account_name:
                company = self.company
                employee = f"{self.first_name} {self.last_name}"
            else:
                company = employee = None
            return ImportedData(
                rule.account_name,
                rule.import_type,
                rule.format,
                company,
                employee,
                self.date,
                paycheck_description(rule.format),
                None,
                Amount(current_income, rule.currency),
                Amount(ytd_income, rule.currency),
                self.filename,
                lineno,
            )
        return None


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


def paycheck_description(fmt: PaycheckFormat):
    if fmt == PaycheckFormat.CSCO_TY2020 or fmt == PaycheckFormat.CSCO_TY2021:
        return "Paycheck - Cisco Systems, Inc."
    return "Paycheck - Unknown company"


def get_info(imported_data: ImportedData) -> Dict[str, Any]:
    return dict(
        type="text/plain",
        filename=imported_data.filename,
        line=imported_data.lineno,
    )


class PrepareState:
    def __init__(
        self, source: "PaycheckSource", journal: JournalEditor, results: SourceResults
    ):
        self.source = source
        self.journal = journal
        self.results = results
        self.rules = []  # type: List[ImportAccountRule]
        self.entry_map = dict()  # type: Dict[str, List[ImportedData]]
        self.matching_transactions = []

        # Find paycheck account rules from paycheck metadata in account
        # Open.meta and mark as 'authoritative' and/or 'skip training account'
        # collect regex to use for extracting entry data.
        for name, account in journal.accounts.items():
            if not isinstance(account, Open):
                continue
            for paycheck_rule in paycheck_rule_map:
                if paycheck_rule.meta_key in account.meta:
                    filename = account.meta["filename"]
                    rule = ImportAccountRule(
                        account.account,
                        paycheck_rule.import_type,
                        paycheck_rule.format,
                        account.currencies[0],
                        paycheck_rule.regex,
                        filename,
                        account.meta.get("lineno"),
                    )
                    self.rules.append(rule)

        # Gather Imported Data from paycheck files
        for rule in self.rules:
            for txtfile in self.source.parsed_files:
                if rule.format != txtfile.format:
                    continue
                account_data = txtfile.apply_account_rule(rule)
                if account_data is not None:
                    self.entry_map.setdefault(account_data.filename, []).append(
                        account_data
                    )

        # Find all matched transactions in journal
        for entry in journal.all_entries:
            if not isinstance(entry, Transaction):
                continue
            for posting in entry.postings:
                if posting.meta is None:
                    continue
                if not PAYCHECK_KEY_FILE in posting.meta:
                    continue
                debugpy.breakpoint()
                import_type = self.find_account_import_type(posting.account)
                if (
                    import_type == ImportedDataType.EXPENSE
                    or import_type == ImportedDataType.INCOME
                ):
                    results.add_account(posting.account)
                if PAYCHECK_KEY_COMPANY in posting.meta:
                    self.matching_transactions.append([entry, posting])

    def find_accoun_import_type(self, account_name):
        for rule in self.rules:
            if rule.account_name == account_name:
                return rule.import_type
        return ImportedDataType.UNKNOWN

    def get_entries(self):
        for file, import_map in sorted(self.entry_map.items()):
            entry = None
            for imported_data in import_map:
                if entry is None:
                    entry = Transaction(
                        meta=None,
                        date=imported_data.date,
                        flag=FLAG_OKAY,
                        payee=None,
                        narration=imported_data.description,
                        tags=EMPTY_SET,
                        links=EMPTY_SET,
                        postings=[],
                    )
                entry.postings.append(imported_data.posting())
            self.results.add_pending_entry(
                ImportResult(
                    date=imported_data.date,
                    entries=[entry],
                    info=get_info(imported_data),
                )
            )


class PaycheckSource(Source):
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
        self.imported_data = dict()
        # Parse paycheck files
        for filename in sorted(self.paycheck_files):
            if not valid_filename(filename):
                self.log_status(f"paycheck: skipping invalid filename: '{filename}'")
                continue
            self.log_status(f"paycheck: parsing TxtFile '{filename}'...")
            txtfile = TxtFile(
                self.log_status, filename, company, first_name, last_name, employee_id
            )
            if txtfile.format == PaycheckFormat.UNKNOWN:
                print(f"ERROR: Unknown file format for {filename}")
                continue
            self.log_status(pformat(txtfile, indent=2))
            self.parsed_files.append(txtfile)

    def get_example_key_value_pairs(self, transaction: Transaction, posting: Posting):
        result = dict()

        def maybe_add_key(key):
            x = posting.meta.get(key)
            if x is not None:
                result[key] = x

        if posting.meta is not None:
            maybe_add_key(PAYCHECK_KEY_FILE)
            if "Salary" in posting.account:
                maybe_add_key(PAYCHECK_KEY_COMPANY)
                maybe_add_key(PAYCHECK_KEY_EMPLOYEE)
        return result

    def is_posting_cleared(self, posting: Posting):
        if posting.meta is None:
            return False
        return PAYCHECK_KEY_FILE in posting.meta

    def prepare(self, journal, results: SourceResults):
        state = PrepareState(self, journal, results)
        state.get_entries()

    @property
    def name(self):
        return "paycheck"


def load(spec, log_status):
    return PaycheckSource(log_status=log_status, **spec)
