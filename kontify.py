#!/usr/bin/python3
# vim: set ts=4 sts=4 sw=4 noexpandtab autoindent smartindent:

#
# kontify.pl - fetch your bank account statements and notify you of new ones
# Copyright (C) 2018 Jakob Hirsch <jh.kontify-2018@plonk.de>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA
#

import sys
import os
from datetime import date, timedelta
import re
import yaml
from decimal import Decimal

import sqlite3
from sqlite3 import IntegrityError

from fints.client import FinTS3PinTanClient, logger
from fints.exceptions import FinTSError
import mt940

from requests.exceptions import RequestException
import urllib.parse
import urllib.request
import json

config = yaml.safe_load(open("kontify.yaml"))

if len(sys.argv) > 1:
	days = int(sys.argv[1])
elif 'days' in config:
	days = config['days']
else:
	days = 7

# helper ########################################
DEBUG = ('DEBUG' in os.environ and os.environ['DEBUG']) or 'debug' in config and config['debug']
DUMMY = ('DUMMY' in os.environ and os.environ['DUMMY']) or 'dummy' in config and config['dummy']
def dprint(*args):
	if DEBUG:
		print(*args)

if 'ignore_responses' in config:
	logger.addFilter(lambda record: 0 if getattr(record, 'fints_response_code', None) in config['ignore_responses'] else 1)

def str_suffix_unless_empty(s, suffix):
	return s + suffix if s else ''

# mt940/fints extensions ########################
# format statement data value
def transaction_formatval(self, k, noneval, amntcur):
	val = s.data.get(k)
	if val is None:
		return noneval
	elif isinstance(val, mt940.models.Amount):
		if amntcur:
			return '%s %s' % (val.amount, val.currency)
		else:
			return val.amount
	else:
		return str(val).strip()

# format for output
def transaction_strval(self, k):
	return self.formatval(k, '', True)

# format for SQL db
def transaction_sqlval(self, k):
	return self.formatval(k, None, False)

mt940.models.Transaction.formatval = transaction_formatval
mt940.models.Transaction.strval = transaction_strval
mt940.models.Transaction.sqlval = transaction_sqlval

# db helper #####################################

# the sqlite module cannot handle Decimal() (see https://stackoverflow.com/a/6319513)
# convert Decimals to TEXT when inserting
def adapt_decimal(d):
	return str(d)

# transparently convert TEXT into Decimals when fetching
def convert_decimal(s):
	return Decimal(s)

sqlite3.register_adapter(Decimal, adapt_decimal)
sqlite3.register_converter("decimal", convert_decimal)

db = sqlite3.connect(config['db']['path'], detect_types=sqlite3.PARSE_DECLTYPES)

def get_accounts(blz, user):
	c = db.cursor()
	c.execute('SELECT number, id FROM account WHERE blz=? AND user=? ', (blz, user))
	return {row[0]: row[1] for row in c}

def add_account(blz, user, accnum):
	if DUMMY:
		return 0
	print('BLZ %s login %s new account %s' % (blz, user, accnum))
	c = db.cursor()
	c.execute('INSERT INTO account (`blz`, `user`, `number`) VALUES (?, ?, ?)', (blz, user, accnum))
	db.commit()
	return c.lastrowid

def add_statement(accid, indaynum, balance, stmt):
	if DUMMY:
		return 1
	c = db.cursor()
	q_cols = ('account_id', 'day', 'amount', 'appl_name', 'appl_iban', 'post_text', 'purpose', 'adtnl_purpose', 'adtnl_pos_ref', 'appl_creditor_id', 'e2e_ref', 'prima_nota', 'return_debit_notes', 'transaction_code', 'intradaynum', 'balance_after')
	q = 'INSERT INTO `statement` (%s) VALUES (%s)' % (
			','.join(q_cols),
			','.join(('?',) * len(q_cols))
		)
	#dprint('add_statement query:', q)
	valkeys = ('date', 'amount', 'applicant_name', 'applicant_iban', 'posting_text', 'purpose', 'additional_purpose', 'additional_position_reference', 'applicant_creditor_id', 'end_to_end_reference', 'prima_nota', 'return_debit_notes', 'transaction_code')
	values = (accid, ) + tuple(stmt.sqlval(k) for k in valkeys) + (indaynum, balance)
	try:
		c.execute(q, values)
		db.commit()
		return c.lastrowid
	except IntegrityError:
		db.rollback()
		return -1

# notify ########################################
def notify(bankname, acc, stmt, balance):
	if 'notify' not in config:
		return
	c = config['notify']
	full_purpose = re.split(' {2,}', stmt.strval('purpose') + ' ' + stmt.strval('add_purpose'))
	if 'stdout' in c:
		print('%s %s (BLZ %s) Konto %s: %s "%s"' % (stmt.strval('date'), bankname, acc.blz, acc.accountnumber, stmt.strval('amount'), stmt.strval('applicant_name')))
		print('%s%s' % (str_suffix_unless_empty(stmt.strval('posting_text'), ': '), ' '.join(full_purpose)))
		print('Neuer Kontostand:', balance)
		print()
	if DUMMY:
		return # skip "real" notifications
	if 'telegram' in c:
		msg = '%(date)s *%(bankname)s* (BLZ %(blz)s)\nKonto *%(accno)s*: *%(amount)s*\n_%(applname)s_%(posttext)s_%(purp)s_\nNeuer Kontostand: *%(balance)s*' % {
			'date': stmt.strval('date'), 'bankname': bankname, 'blz': acc.blz,
			'accno': acc.accountnumber, 'amount': stmt.strval('amount'),
			'applname': str_suffix_unless_empty(stmt.strval('applicant_name'), '\n'),
			'posttext': str_suffix_unless_empty(stmt.strval('posting_text'), ':\n'),
			'purp': '\n'.join(full_purpose),
			'balance': balance
		}
		sendtelegrammessage(msg)

def sendtelegrammessage(msg):
	try:
		c = config['notify']['telegram']
		url = 'https://api.telegram.org/bot%s/sendMessage' % (c['bottoken'],)
		data = urllib.parse.urlencode({ 'chat_id': c['chatid'], 'text': msg, 'parse_mode': 'Markdown', 'disable_web_page_preview': True }).encode('ascii')
		response = urllib.request.urlopen(url, data).read()
		res = json.loads(response)
		return res['ok']
	except Exception as e:
		print('sending telegram message failed:', e)
		return False

# main ##########################################

for l in config['login']:
	try:
		blz, user, pin = (l[k] for k in ('blz', 'user', 'pin'))
	except KeyError as e:
		print('! missing login config key %s in %s' % (e, str(l)))
		continue
	try:
		bankname, url = (config['access'][blz][k] for k in ('name', 'url'))
	except KeyError:
		print('! missing access config for BLZ %s' % (blz,))
		continue
	dprint("* %s (blz %s) user %s" % (bankname, blz, user))
	try:
		f = FinTS3PinTanClient(blz, user, pin, url)
		accounts = f.get_sepa_accounts()
	except (FinTSError, RequestException, ValueError) as e:
		print("! fints client exception for %s (blz %s) user %s: %s" % (bankname, blz, user, e))
		continue
	accountlist = get_accounts(blz, user)
	for a in accounts:
		if not a.iban:
			continue
		if 'only' in l and a.accountnumber not in l['only']:
			continue
		if 'ignore' in l and a.accountnumber in l['ignore']:
			continue
		if a.accountnumber not in accountlist:
			accountlist[a.accountnumber] = add_account(blz, user, a.accountnumber)
		accid = accountlist[a.accountnumber]

		dprint("** [%s] account %s (IBAN %s BIC %s)" % (accid, a.accountnumber, a.iban, a.bic))
		if days >= 0:
			try:
				statement = f.get_transactions(a, date.today() - timedelta(days), date.today())
			except (FinTSError, RequestException) as e:
				print("! fints get_transactions exception for %s (blz %s) user %s account %s: %s" % (bankname, blz, user, a.accountnumber, e))
				continue
			if not statement:
				continue

			cnt_added = 0
			cnt_dupl = 0

			t = statement[0].transactions
			balance_closing = t.data.get('final_closing_balance').amount.amount
			# for some reason, the opening balance is wrong for some banks (e.g. VoBa Ortenau), but the closing balance is correct, so we calculate the balance backwards...
			balance = balance_closing - sum(s.data['amount'].amount for s in statement)
			dprint('  balance: opening %s, closing %s' % (balance, balance_closing))
			day0 = None
			for s in statement:
				balance += s.data['amount'].amount
				dprint(' * %s  %s  "%s" (%s)  new balance: %s' % (tuple(s.strval(k) for k in ('date', 'amount', 'applicant_name', 'applicant_iban')) + (balance,)))
				dprint('   %s: "%s"' % tuple(s.strval(k) for k in ('posting_text', 'purpose')))
				day = s.data['date']
				if day == day0:
					indaynum += 1
				else:
					indaynum = 1
					day0 = day
				if add_statement(accid, indaynum, balance, s) > 0:
					cnt_added += 1
					notify(bankname, a, s, balance)
				else:
					dprint('   - transaction already in database')
					cnt_dupl += 1
			dprint(" + %d statements, %d new, %d known " % (len(statement), cnt_added, cnt_dupl))
		dprint()

