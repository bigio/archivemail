#! /usr/bin/env python
############################################################################
# Copyright (C) 2002  Paul Rodger <paul@paulrodger.com>
#           (C) 2006-2008  Nikolaus Schulz <microschulz@web.de>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
############################################################################
"""
Unit-test archivemail using 'PyUnit'.

TODO: add tests for:
    * dotlock locks already existing
    * archiving maildir-format mailboxes
    * archiving MH-format mailboxes
    * preservation of status information from maildir to mbox
    * a 3rd party process changing the mbox file being read

"""

import sys

def check_python_version(): 
    """Abort if we are running on python < v2.3"""
    too_old_error = "This test script requires python version 2.3 or later. " + \
      "Your version of python is:\n%s" % sys.version
    try: 
        version = sys.version_info  # we might not even have this function! :)
        if (version[0] < 2) or (version[0] == 2 and version[1] < 3):
            print too_old_error
            sys.exit(1)
    except AttributeError:
        print too_old_error
        sys.exit(1)

# define & run this early because 'unittest' requires Python >= 2.1
check_python_version()  

import copy
import fcntl
import filecmp
import os
import re
import shutil
import stat
import tempfile
import time
import unittest
import gzip
import cStringIO
import rfc822

try:
    import archivemail
except ImportError:
    print "The archivemail script needs to be called 'archivemail.py'"
    print "and should be in the current directory in order to be imported"
    print "and tested. Sorry."
    if os.path.isfile("archivemail"):
        print "Try renaming it from 'archivemail' to 'archivemail.py'."
    sys.exit(1)

# precision of os.utime() when restoring mbox timestamps
utimes_precision = 5


class TestCaseInTempdir(unittest.TestCase):
    """Base class for testcases that need to create temporary files. 
    All testcases that create temporary files should be derived from this
    class, not directly from unittest.TestCase.
    TestCaseInTempdir provides these methods:
    
    setUp()     Creates a safe temporary directory and sets tempfile.tempdir.
                
    tearDown()  Recursively removes the temporary directory and unsets
                tempfile.tempdir.

    Overriding methods should call the ones above."""
    temproot = None

    def setUp(self):
        if not self.temproot:
            assert not tempfile.tempdir
            self.temproot = tempfile.tempdir = \
                tempfile.mkdtemp(prefix="test-archivemail")
     
    def tearDown(self):
        assert tempfile.tempdir == self.temproot
        if self.temproot:
            shutil.rmtree(self.temproot)
            tempfile.tempdir = self.temproot = None


############ Mbox Class testing ##############

class TestMboxDotlock(TestCaseInTempdir):
    def setUp(self):
        super(TestMboxDotlock, self).setUp()
        self.mbox_name = make_mbox()
        self.mbox_mode = os.stat(self.mbox_name)[stat.ST_MODE]
        self.mbox = archivemail.Mbox(self.mbox_name)

    def testDotlock(self):
        """dotlock_lock/unlock should create/delete a lockfile"""
        lock = self.mbox_name + ".lock"
        self.mbox._dotlock_lock()
        assert os.path.isfile(lock)
        self.mbox._dotlock_unlock()
        assert not os.path.isfile(lock)

    def testDotlockingSucceedsUponEACCES(self):
        """A dotlock should silently be omitted upon EACCES."""
        archivemail.options.quiet = True
        mbox_dir = os.path.dirname(self.mbox_name)
        os.chmod(mbox_dir, 0500)
        try:
            self.mbox._dotlock_lock()
        finally:
            os.chmod(mbox_dir, 0700)
            archivemail.options.quiet = False

class TestMboxPosixLock(TestCaseInTempdir):
    def setUp(self):
        super(TestMboxPosixLock, self).setUp()
        self.mbox_name = make_mbox()
        self.mbox = archivemail.Mbox(self.mbox_name)

    def testPosixLock(self):
        """posix_lock/unlock should create/delete an advisory lock"""
        
        # The following code snippet heavily lends from the Python 2.5 mailbox
        # unittest.
        # BEGIN robbery:

        # Fork off a subprocess that will lock the file for 2 seconds,
        # unlock it, and then exit.
        pid = os.fork()
        if pid == 0:
            # In the child, lock the mailbox.
            self.mbox._posix_lock()
            time.sleep(2)
            self.mbox._posix_unlock()
            os._exit(0)

        # In the parent, sleep a bit to give the child time to acquire
        # the lock.
        time.sleep(0.5)
        # The parent's file self.mbox.mbox_file shares fcntl locks with the
        # duplicated FD in the child; reopen it so we get a different file
        # table entry.
        file = open(self.mbox_name, "r+")
        lock_nb = fcntl.LOCK_EX | fcntl.LOCK_NB
        fd = file.fileno()
        try:
            self.assertRaises(IOError, fcntl.lockf, fd, lock_nb)

        finally:
            # Wait for child to exit.  Locking should now succeed.
            exited_pid, status = os.waitpid(pid, 0)

        fcntl.lockf(fd, lock_nb)
        fcntl.lockf(fd, fcntl.LOCK_UN)
        # END robbery


class TestMboxNext(TestCaseInTempdir):
    def setUp(self):
        super(TestMboxNext, self).setUp()
        self.not_empty_name = make_mbox(messages=18)
        self.empty_name = make_mbox(messages=0)

    def testNextEmpty(self):
        """mbox.next() should return None on an empty mailbox"""
        mbox = archivemail.Mbox(self.empty_name)
        msg = mbox.next()
        self.assertEqual(msg, None)

    def testNextNotEmpty(self):
        """mbox.next() should a message on a populated mailbox"""
        mbox = archivemail.Mbox(self.not_empty_name)
        for count in range(18):
            msg = mbox.next()
            assert msg
        msg = mbox.next()
        self.assertEqual(msg, None)


############ TempMbox Class testing ##############

class TestTempMboxWrite(TestCaseInTempdir):
    def setUp(self):
        super(TestTempMboxWrite, self).setUp()

    def testWrite(self):
        """mbox.write() should append messages to a mbox mailbox"""
        read_file = make_mbox(messages=3)
        mbox_read = archivemail.Mbox(read_file)
        mbox_write = archivemail.TempMbox()
        write_file = mbox_write.mbox_file_name
        for count in range(3):
            msg = mbox_read.next()
            mbox_write.write(msg)
        mbox_read.close()
        mbox_write.close()
        assert filecmp.cmp(read_file, write_file, shallow=0)

    def testWriteNone(self):
        """calling mbox.write() with no message should raise AssertionError"""
        write = archivemail.TempMbox()
        self.assertRaises(AssertionError, write.write, None)

class TestTempMboxRemove(TestCaseInTempdir):
    def setUp(self):
        super(TestTempMboxRemove, self).setUp()
        self.mbox = archivemail.TempMbox()
        self.mbox_name = self.mbox.mbox_file_name

    def testMboxRemove(self):
        """remove() should delete a mbox mailbox"""
        assert os.path.exists(self.mbox_name)
        self.mbox.remove()
        assert not os.path.exists(self.mbox_name)



########## options class testing #################

class TestOptionDefaults(unittest.TestCase):
    def testVerbose(self):
        """verbose should be off by default"""
        self.assertEqual(archivemail.options.verbose, False)

    def testDaysOldMax(self):
        """default archival time should be 180 days"""
        self.assertEqual(archivemail.options.days_old_max, 180)

    def testQuiet(self):
        """quiet should be off by default"""
        self.assertEqual(archivemail.options.quiet, False)

    def testDeleteOldMail(self):
        """we should not delete old mail by default"""
        self.assertEqual(archivemail.options.delete_old_mail, False)

    def testNoCompress(self):
        """no-compression should be off by default"""
        self.assertEqual(archivemail.options.no_compress, False)

    def testIncludeFlagged(self):
        """we should not archive flagged messages by default"""
        self.assertEqual(archivemail.options.include_flagged, False)

    def testPreserveUnread(self):
        """we should not preserve unread messages by default"""
        self.assertEqual(archivemail.options.preserve_unread, False)

class TestOptionParser(unittest.TestCase):
    def setUp(self):
        self.oldopts = copy.copy(archivemail.options)

    def testOptionDate(self):
        """--date and -D options are parsed correctly"""
        date_formats = (
            "%Y-%m-%d",  # ISO format
            "%d %b %Y" , # Internet format
            "%d %B %Y" , # Internet format with full month names
        )
        date = time.strptime("2000-07-29", "%Y-%m-%d")
        unixdate = time.mktime(date)
        for df in date_formats:
            d = time.strftime(df, date)
            for opt in '-D', '--date=':
                archivemail.options.date_old_max = None
                archivemail.options.parse_args([opt+d], "")
                self.assertEqual(unixdate, archivemail.options.date_old_max)

    def testOptionPreserveUnread(self):
        """--preserve-unread option is parsed correctly"""
        archivemail.options.parse_args(["--preserve-unread"], "")
        assert archivemail.options.preserve_unread
        archivemail.options.preserve_unread = False
        archivemail.options.parse_args(["-u"], "")
        assert archivemail.options.preserve_unread

    def testOptionSuffix(self):
        """--suffix and -s options are parsed correctly"""
        for suffix in ("_static_", "_%B_%Y", "-%Y-%m-%d"):
            archivemail.options.parse_args(["--suffix="+suffix], "")
            assert archivemail.options.archive_suffix == suffix
            archivemail.options.suffix = None
            archivemail.options.parse_args(["-s", suffix], "")
            assert archivemail.options.archive_suffix == suffix

    def testOptionDryrun(self):
        """--dry-run option is parsed correctly"""
        archivemail.options.parse_args(["--dry-run"], "")
        assert archivemail.options.dry_run
        archivemail.options.preserve_unread = False
        archivemail.options.parse_args(["-n"], "")
        assert archivemail.options.dry_run

    def testOptionDays(self):
        """--days and -d options are parsed correctly"""
        archivemail.options.parse_args(["--days=11"], "")
        self.assertEqual(archivemail.options.days_old_max, 11)
        archivemail.options.days_old_max = None
        archivemail.options.parse_args(["-d11"], "")
        self.assertEqual(archivemail.options.days_old_max, 11)

    def testOptionDelete(self):
        """--delete option is parsed correctly"""
        archivemail.options.parse_args(["--delete"], "")
        assert archivemail.options.delete_old_mail

    def testOptionCopy(self):
        """--copy option is parsed correctly"""
        archivemail.options.parse_args(["--copy"], "")
        assert archivemail.options.copy_old_mail

    def testOptionOutputdir(self):
        """--output-dir and -o options are parsed correctly"""
        for path in "/just/some/path", "relative/path":
            archivemail.options.parse_args(["--output-dir=%s" % path], "")
            self.assertEqual(archivemail.options.output_dir, path)
            archivemail.options.output_dir = None
            archivemail.options.parse_args(["-o%s" % path], "")
            self.assertEqual(archivemail.options.output_dir, path)

    def testOptionNocompress(self):
        """--no-compress option is parsed correctly"""
        archivemail.options.parse_args(["--no-compress"], "")
        assert archivemail.options.no_compress

    def testOptionSize(self):
        """--size and -S options are parsed correctly"""
        size = "666"
        archivemail.options.parse_args(["--size=%s" % size ], "")
        self.assertEqual(archivemail.options.min_size, int(size))
        archivemail.options.parse_args(["-S%s" % size ], "")
        self.assertEqual(archivemail.options.min_size, int(size))

    def tearDown(self):
        archivemail.options = self.oldopts

########## archivemail.is_older_than_days() unit testing #################

class TestIsTooOld(unittest.TestCase):
    def testVeryOld(self):
        """with max_days=360, should be true for these dates > 1 year"""
        for years in range(1, 10):
            time_msg = time.time() - (years * 365 * 24 * 60 * 60)
            assert archivemail.is_older_than_days(time_message=time_msg,
                max_days=360)

    def testOld(self):
        """with max_days=14, should be true for these dates > 14 days"""
        for days in range(14, 360):
            time_msg = time.time() - (days * 24 * 60 * 60)
            assert archivemail.is_older_than_days(time_message=time_msg,
                max_days=14)

    def testJustOld(self):
        """with max_days=1, should be true for these dates >= 1 day"""
        for minutes in range(0, 61):
            time_msg = time.time() - (25 * 60 * 60) + (minutes * 60)
            assert archivemail.is_older_than_days(time_message=time_msg,
                max_days=1)

    def testNotOld(self):
        """with max_days=9, should be false for these dates < 9 days"""
        for days in range(0, 9):
            time_msg = time.time() - (days * 24 * 60 * 60)
            assert not archivemail.is_older_than_days(time_message=time_msg,
                max_days=9)

    def testJustNotOld(self):
        """with max_days=1, should be false for these hours <= 1 day"""
        for minutes in range(0, 60):
            time_msg = time.time() - (23 * 60 * 60) - (minutes * 60)
            assert not archivemail.is_older_than_days(time_message=time_msg,
                max_days=1)

    def testFuture(self):
        """with max_days=1, should be false for times in the future"""
        for minutes in range(0, 60):
            time_msg = time.time() + (minutes * 60)
            assert not archivemail.is_older_than_days(time_message=time_msg,
                max_days=1)

########## archivemail.parse_imap_url() unit testing #################

class TestParseIMAPUrl(unittest.TestCase): 
    def setUp(self):
        archivemail.options.quiet = True
        archivemail.options.verbose = False
        archivemail.options.pwfile = None
        
    urls_withoutpass = [
            ('imaps://user@example.org@imap.example.org/upperbox/lowerbox',
                ('user', None, 'example.org@imap.example.org',
                'upperbox/lowerbox')), 
            ('imaps://"user@example.org"@imap.example.org/upperbox/lowerbox',
                ('user@example.org', None, 'imap.example.org',
                'upperbox/lowerbox')), 
            ('imaps://user@example.org"@imap.example.org/upperbox/lowerbox',
                ('user', None, 'example.org"@imap.example.org',
                'upperbox/lowerbox')), 
            ('imaps://"user@example.org@imap.example.org/upperbox/lowerbox',
                ('"user', None, 'example.org@imap.example.org',
                'upperbox/lowerbox')), 
            ('imaps://"us\\"er@example.org"@imap.example.org/upperbox/lowerbox',
                ('us"er@example.org', None, 'imap.example.org',
                'upperbox/lowerbox')), 
            ('imaps://user\\@example.org@imap.example.org/upperbox/lowerbox',
                ('user\\', None, 'example.org@imap.example.org',
                'upperbox/lowerbox'))
    ]
    urls_withpass = [
            ('imaps://user@example.org:passwd@imap.example.org/upperbox/lowerbox',
                ('user@example.org', 'passwd', 'imap.example.org',
                'upperbox/lowerbox'), 
                ('user', None, 'example.org:passwd@imap.example.org',
                'upperbox/lowerbox')), 
            ('imaps://"user@example.org:passwd@imap.example.org/upperbox/lowerbox',
                ('"user@example.org', "passwd", 'imap.example.org',
                'upperbox/lowerbox'), 
                ('"user', None, 'example.org:passwd@imap.example.org',
                'upperbox/lowerbox')), 
            ('imaps://u\\ser\\@example.org:"p@sswd"@imap.example.org/upperbox/lowerbox', 
                ('u\\ser\\@example.org', 'p@sswd', 'imap.example.org',
                'upperbox/lowerbox'),
                ('u\\ser\\', None, 'example.org:"p@sswd"@imap.example.org',
                'upperbox/lowerbox'))
    ]
    # These are invalid when the password's not stripped. 
    urls_onlywithpass = [
            ('imaps://"user@example.org":passwd@imap.example.org/upperbox/lowerbox',
                ('user@example.org', "passwd", 'imap.example.org',
                'upperbox/lowerbox'))
    ]
    def testUrlsWithoutPwfile(self):
        """Parse test urls with --pwfile option unset. This parses a password in
        the URL, if present."""
        archivemail.options.pwfile = None
        for mbstr in self.urls_withpass + self.urls_withoutpass:
            url = mbstr[0][mbstr[0].find('://')+3:]
            result = archivemail.parse_imap_url(url)
            self.assertEqual(result, mbstr[1])

    def testUrlsWithPwfile(self):
        """Parse test urls with --pwfile set.  In this case the ':' character
        loses its meaning as a delimiter."""
        archivemail.options.pwfile = "whocares.txt"
        for mbstr in self.urls_withpass: 
            url = mbstr[0][mbstr[0].find('://')+3:]
            result = archivemail.parse_imap_url(url)
            self.assertEqual(result, mbstr[2])
        for mbstr in self.urls_onlywithpass: 
            url = mbstr[0][mbstr[0].find('://')+3:]
            self.assertRaises(archivemail.UnexpectedError,
                    archivemail.parse_imap_url, url)

    def tearDown(self): 
        archivemail.options.quiet = False
        archivemail.options.verbose = False
        archivemail.options.pwfile = None

########## acceptance testing ###########

class TestArchive(TestCaseInTempdir):
    """Base class defining helper functions for doing test archiving runs."""
    mbox = None         # mbox file that will be processed by archivemail
    good_archive = None # Uncompressed reference archive file to verify the
                        # archive after processing
    good_mbox = None    # Reference mbox file to verify the mbox after processing

    def verify(self):
        assert os.path.exists(self.mbox)
        if self.good_mbox is not None:
            assertEqualContent(self.mbox, self.good_mbox)
        else:
            self.assertEqual(os.path.getsize(self.mbox), 0)
        archive_name = self.mbox + "_archive"
        if not archivemail.options.no_compress:
            archive_name += ".gz"
            iszipped = True
        else:
            assert not os.path.exists(archive_name + ".gz")
            iszipped = False
        if self.good_archive is not None:
            assertEqualContent(archive_name, self.good_archive, iszipped)
        else:
            assert not os.path.exists(archive_name)

    def make_old_mbox(self, body=None, headers=None, messages=1, make_old_archive=False):
        """Prepare for a test run with an old mbox by making an old mbox,
        optionally an existing archive, and a reference archive to verify the
        archive after archivemail has run."""
        self.mbox = make_mbox(body, headers, 181*24, messages)
        archive_does_change = not (archivemail.options.dry_run or
                archivemail.options.delete_old_mail)
        mbox_does_not_change = archivemail.options.dry_run or \
                archivemail.options.copy_old_mail
        if make_old_archive:
            archive = archivemail.make_archive_name(self.mbox)
            self.good_archive = make_archive_and_plain_copy(archive)
            if archive_does_change:
                append_file(self.mbox, self.good_archive)
        elif archive_does_change:
            self.good_archive = tempfile.mkstemp()[1]
            shutil.copyfile(self.mbox, self.good_archive)
        if mbox_does_not_change:
            if archive_does_change and not make_old_archive:
                self.good_mbox = self.good_archive
            else:
                self.good_mbox = tempfile.mkstemp()[1]
                shutil.copyfile(self.mbox, self.good_mbox)

    def make_mixed_mbox(self, body=None, headers=None, messages=1, make_old_archive=False):
        """Prepare for a test run with a mixed mbox by making a mixed mbox,
        optionally an existing archive, a reference archive to verify the
        archive after archivemail has run, and likewise a reference mbox to
        verify the mbox."""
        self.make_old_mbox(body, headers, messages=messages, make_old_archive=make_old_archive)
        new_mbox_name = make_mbox(body, headers, 179*24, messages)
        append_file(new_mbox_name, self.mbox)
        if self.good_mbox is None:
            self.good_mbox = new_mbox_name
        else:
            if self.good_mbox == self.good_archive:
                self.good_mbox = tempfile.mkstemp()[1]
                shutil.copyfile(self.mbox, self.good_mbox)
            else:
                append_file(new_mbox_name, self.good_mbox)

    def make_new_mbox(self, body=None, headers=None, messages=1, make_old_archive=False):
        """Prepare for a test run with a new mbox by making a new mbox,
        optionally an exiting archive, and a reference mbox to verify the mbox
        after archivemail has run."""
        self.mbox = make_mbox(body, headers, 179*24, messages)
        self.good_mbox = tempfile.mkstemp()[1]
        shutil.copyfile(self.mbox, self.good_mbox)
        if make_old_archive:
            archive = archivemail.make_archive_name(self.mbox)
            self.good_archive = make_archive_and_plain_copy(archive)


class TestArchiveMbox(TestArchive):
    """archiving should work based on the date of messages given"""

    def setUp(self):
        self.oldopts = copy.copy(archivemail.options)
        archivemail.options.quiet = True
        super(TestArchiveMbox, self).setUp()
 
    def testOld(self):
        """archiving an old mailbox"""
        self.make_old_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def testOldFromInBody(self):
        """archiving an old mailbox with 'From ' in the body"""
        body = """This is a message with ^From at the start of a line
From is on this line
This is after the ^From line"""
        self.make_old_mbox(messages=3, body=body)
        archivemail.archive(self.mbox)
        self.verify()

    def testDateSystem(self):
        """test that the --date option works as expected"""
        test_headers = (
            {
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2030',
                'Date' : 'Fri, 28 Jul 2000 16:11:36 +1000',
            },
            {
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2000',
                'Date' : None,
            },
            {
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2030',
                'Date' : None,
                'Delivery-date' : 'Fri, 28 Jul 2000 16:11:36 +1000',
            },
            {
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2030',
                'Date' : None,
                'Resent-Date' : 'Fri, 28 Jul 2000 16:11:36 +1000',
            },
        )
        for headers in test_headers:
            msg = make_message(default_headers=headers, wantobj=True)
            date = time.strptime("2000-07-29", "%Y-%m-%d")
            archivemail.options.date_old_max = time.mktime(date)
            assert archivemail.should_archive(msg)
            date = time.strptime("2000-07-27", "%Y-%m-%d")
            archivemail.options.date_old_max = time.mktime(date)
            assert not archivemail.should_archive(msg)

    def testMixed(self):
        """archiving a mixed mailbox"""
        self.make_mixed_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def testNew(self):
        """archiving a new mailbox"""
        self.make_new_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def testOldExisting(self):
        """archiving an old mailbox with an existing archive"""
        self.make_old_mbox(messages=3, make_old_archive=True)
        archivemail.archive(self.mbox)
        self.verify()

    def testOldWeirdHeaders(self):
        """archiving old mailboxes with weird headers"""
        weird_headers = (
            {   # we should archive because of the date on the 'From_' line
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2000',
                'Date'  : 'Friskhdfkjkh, 28 Jul 2002 1line noise6:11:36 +1000',
            },
            {   # we should archive because of the date on the 'From_' line
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2000',
                'Date'  : None,
            },
            {   # we should archive because of the date in 'Delivery-date'
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2030',
                'Date'  : 'Frcorruptioni, 28 Jul 20line noise00 16:6 +1000',
                'Delivery-date' : 'Fri, 28 Jul 2000 16:11:36 +1000',
            },
            {   # we should archive because of the date in 'Delivery-date'
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2030',
                'Date' : None,
                'Delivery-date' : 'Fri, 28 Jul 2000 16:11:36 +1000',
            },
            {   # we should archive because of the date in 'Resent-Date'
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2030',
                'Date'  : 'Frcorruptioni, 28 Jul 20line noise00 16:6 +1000',
                'Resent-Date' : 'Fri, 28 Jul 2000 16:11:36 +1000',
            },
            {   # we should archive because of the date in 'Resent-Date'
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2030',
                'Date' : None,
                'Resent-Date' : 'Fri, 28 Jul 2000 16:11:36 +1000',
            },
            {   # completely blank dates were crashing < version 0.4.7
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2000',
                'Date'  : '',
            },
            {   # completely blank dates were crashing < version 0.4.7
                'From_' : 'sender@dummy.domain Fri Jul 28 16:11:36 2000',
                'Date'  : '',
                'Resent-Date'  : '',
            },
        )
        fd, self.mbox = tempfile.mkstemp()
        fp = os.fdopen(fd, "w")
        for headers in weird_headers:
            msg_text = make_message(default_headers=headers)
            fp.write(msg_text*2)
        fp.close()
        self.good_archive = tempfile.mkstemp()[1]
        shutil.copyfile(self.mbox, self.good_archive)
        archivemail.archive(self.mbox)
        self.verify()

    def tearDown(self):
        archivemail.options = self.oldopts
        super(TestArchiveMbox, self).tearDown()


class TestArchiveMboxTimestamp(TestCaseInTempdir):
    """original mbox timestamps should always be preserved"""
    def setUp(self):
        super(TestArchiveMboxTimestamp, self).setUp() 
        archivemail.options.quiet = True
        self.mbox_name = make_mbox(messages=3, hours_old=(24 * 180))
        self.mtime = os.path.getmtime(self.mbox_name) - 66
        self.atime = os.path.getatime(self.mbox_name) - 88
        os.utime(self.mbox_name, (self.atime, self.mtime))

    def testNew(self):
        """mbox timestamps should not change after no archival"""
        archivemail.options.days_old_max = 181
        archivemail.archive(self.mbox_name)
        self.verify()

    def testOld(self):
        """mbox timestamps should not change after archival"""
        archivemail.options.days_old_max = 179
        archivemail.archive(self.mbox_name)
        self.verify()

    def verify(self):
        assert os.path.exists(self.mbox_name)
        new_atime = os.path.getatime(self.mbox_name)
        new_mtime = os.path.getmtime(self.mbox_name)
        self.assertAlmostEqual(self.mtime, new_mtime, utimes_precision)
        self.assertAlmostEqual(self.atime, new_atime, utimes_precision)

    def tearDown(self):
        archivemail.options.quiet = False
        archivemail.options.days_old_max = 180
        os.remove(self.mbox_name)
        super(TestArchiveMboxTimestamp, self).tearDown()


class TestArchiveMboxAll(unittest.TestCase):
    def setUp(self):
        archivemail.options.quiet = True
        archivemail.options.archive_all = True

    def testNew(self):
        """new messages should be archived with --all"""
        self.msg = make_message(hours_old=24*179, wantobj=True)
        assert archivemail.should_archive(self.msg)

    def testOld(self):
        """old messages should be archived with --all"""
        self.msg = make_message(hours_old=24*181, wantobj=True)
        assert archivemail.should_archive(self.msg)

    def tearDown(self):
        archivemail.options.quiet = False
        archivemail.options.archive_all = False

class TestArchiveMboxPreserveUnread(unittest.TestCase):
    """make sure the 'preserve_unread' option works"""
    def setUp(self):
        archivemail.options.quiet = True
        archivemail.options.preserve_unread = True
        self.msg = make_message(hours_old=24*181, wantobj=True)

    def testOldRead(self):
        """old read messages should be archived with --preserve-unread"""
        self.msg["Status"] = "RO"
        assert archivemail.should_archive(self.msg)

    def testOldUnread(self):
        """old unread messages should not be archived with --preserve-unread"""
        self.msg["Status"] = "O"
        assert not archivemail.should_archive(self.msg)

    def tearDown(self):
        archivemail.options.quiet = False
        archivemail.options.preserve_unread = False


class TestArchiveMboxSuffix(unittest.TestCase):
    """make sure the 'suffix' option works"""
    def setUp(self):
        self.old_suffix = archivemail.options.archive_suffix
        archivemail.options.quiet = True

    def testSuffix(self):
        """archiving with specified --suffix arguments"""
        for suffix in ("_static_", "_%B_%Y", "-%Y-%m-%d"):
            mbox_name = "foobar"
            archivemail.options.archive_suffix = suffix
            days_old_max = 180
            parsed_suffix_time = time.time() - days_old_max*24*60*60
            parsed_suffix = time.strftime(suffix,
                time.localtime(parsed_suffix_time))
            archive_name = mbox_name + parsed_suffix
            self.assertEqual(archive_name,
                    archivemail.make_archive_name(mbox_name))

    def tearDown(self):
        archivemail.options.quiet = False
        archivemail.options.archive_suffix = self.old_suffix


class TestArchiveDryRun(TestArchive):
    """make sure the 'dry-run' option works"""
    def setUp(self):
        super(TestArchiveDryRun, self).setUp()
        archivemail.options.quiet = True
        archivemail.options.dry_run = True

    def testOld(self):
        """archiving an old mailbox with the 'dry-run' option"""
        self.make_old_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def tearDown(self):
        archivemail.options.dry_run = False
        archivemail.options.quiet = False
        super(TestArchiveDryRun, self).tearDown()


class TestArchiveDelete(TestArchive):
    """make sure the 'delete' option works"""
    def setUp(self):
        super(TestArchiveDelete, self).setUp()
        archivemail.options.quiet = True
        archivemail.options.delete_old_mail = True

    def testNew(self):
        """archiving a new mailbox with the 'delete' option"""
        self.make_new_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def testMixed(self):
        """archiving a mixed mailbox with the 'delete' option"""
        self.make_mixed_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def testOld(self):
        """archiving an old mailbox with the 'delete' option"""
        self.make_old_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def tearDown(self):
        archivemail.options.delete_old_mail = False
        archivemail.options.quiet = False
        super(TestArchiveDelete, self).tearDown()


class TestArchiveCopy(TestArchive):
    """make sure the 'copy' option works"""
    def setUp(self):
        super(TestArchiveCopy, self).setUp()
        archivemail.options.quiet = True
        archivemail.options.copy_old_mail = True

    def testNew(self):
        """archiving a new mailbox with the 'copy' option"""
        self.make_new_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def testMixed(self):
        """archiving a mixed mailbox with the 'copy' option"""
        self.make_mixed_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def testOld(self):
        """archiving an old mailbox with the 'copy' option"""
        self.make_old_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def tearDown(self):
        archivemail.options.copy_old_mail = False
        archivemail.options.quiet = False
        super(TestArchiveCopy, self).tearDown()


class TestArchiveMboxFlagged(unittest.TestCase):
    """make sure the 'include_flagged' option works"""
    def setUp(self):
        archivemail.options.include_flagged = False
        archivemail.options.quiet = True

    def testOld(self):
        """by default, old flagged messages should not be archived"""
        msg = make_message(default_headers={"X-Status": "F"},
                hours_old=24*181, wantobj=True)
        assert not archivemail.should_archive(msg)

    def testIncludeFlaggedNew(self):
        """new flagged messages should not be archived with include_flagged"""
        msg = make_message(default_headers={"X-Status": "F"},
                hours_old=24*179, wantobj=True)
        assert not archivemail.should_archive(msg)

    def testIncludeFlaggedOld(self):
        """old flagged messages should be archived with include_flagged"""
        archivemail.options.include_flagged = True
        msg = make_message(default_headers={"X-Status": "F"},
                hours_old=24*181, wantobj=True)
        assert archivemail.should_archive(msg)

    def tearDown(self):
        archivemail.options.include_flagged = False
        archivemail.options.quiet = False


class TestArchiveMboxOutputDir(unittest.TestCase):
    """make sure that the 'output-dir' option works"""
    def setUp(self):
        archivemail.options.quiet = True

    def testOld(self):
        """archiving an old mailbox with a sepecified output dir"""
        for dir in "/just/a/path", "relative/path":
            archivemail.options.output_dir = dir
            archive_dir = archivemail.make_archive_name("/tmp/mbox")
            self.assertEqual(dir, os.path.dirname(archive_dir))

    def tearDown(self):
        archivemail.options.quiet = False
        archivemail.options.output_dir = None


class TestArchiveMboxUncompressed(TestArchive):
    """make sure that the 'no_compress' option works"""
    mbox_name = None
    new_mbox = None
    old_mbox = None
    copy_name = None

    def setUp(self):
        archivemail.options.quiet = True
        archivemail.options.no_compress = True
        super(TestArchiveMboxUncompressed, self).setUp()

    def testOld(self):
        """archiving an old mailbox uncompressed"""
        self.make_old_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def testNew(self):
        """archiving a new mailbox uncompressed"""
        self.make_new_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def testMixed(self):
        """archiving a mixed mailbox uncompressed"""
        self.make_mixed_mbox(messages=3)
        archivemail.archive(self.mbox)
        self.verify()

    def testOldExists(self):
        """archiving an old mailbox uncopressed with an existing archive"""
        self.make_old_mbox(messages=3, make_old_archive=True)
        archivemail.archive(self.mbox)
        self.verify()

    def tearDown(self):
        archivemail.options.quiet = False
        archivemail.options.no_compress = False
        super(TestArchiveMboxUncompressed, self).tearDown()


class TestArchiveSize(unittest.TestCase):
    """check that the 'size' argument works"""
    def setUp(self):
        archivemail.options.quiet = True
        msg_text = make_message(hours_old=24*181)
        self.msg_size = len(msg_text)
        fp = cStringIO.StringIO(msg_text)
        self.msg = rfc822.Message(fp)

    def testSmaller(self):
        """giving a size argument smaller than the message"""
        archivemail.options.min_size = self.msg_size - 1
        assert archivemail.should_archive(self.msg)

    def testBigger(self):
        """giving a size argument bigger than the message"""
        archivemail.options.min_size = self.msg_size + 1
        assert not archivemail.should_archive(self.msg)

    def tearDown(self):
        archivemail.options.quiet = False
        archivemail.options.min_size = None


########## helper routines ############

def make_message(body=None, default_headers={}, hours_old=None, wantobj=False):
    headers = copy.copy(default_headers)
    if not headers:
        headers = {}
    if not headers.has_key('Date'):
        time_message = time.time() - (60 * 60 * hours_old)
        headers['Date'] = time.asctime(time.localtime(time_message))
    if not headers.has_key('From'):
        headers['From'] = "sender@dummy.domain"        
    if not headers.has_key('To'):
        headers['To'] = "receipient@dummy.domain"        
    if not headers.has_key('Subject'):
        headers['Subject'] = "This is the subject"
    if not headers.has_key('From_'):
        headers['From_'] = "%s %s" % (headers['From'], headers['Date'])
    if not body:
        body = "This is the message body"

    msg = ""
    if headers.has_key('From_'):
        msg = msg + ("From %s\n" % headers['From_'])
        del headers['From_']
    for key in headers.keys():
        if headers[key] is not None:
            msg = msg + ("%s: %s\n" % (key, headers[key]))
    msg = msg + "\n\n" + body + "\n\n"
    if not wantobj:
        return msg
    fp = cStringIO.StringIO(msg)
    return rfc822.Message(fp)

def append_file(source, dest):
    """appends the file named 'source' to the file named 'dest'"""
    assert os.path.isfile(source)
    assert os.path.isfile(dest)
    read = open(source, "r")
    write = open(dest, "a+")
    shutil.copyfileobj(read,write)
    read.close()
    write.close()


def make_mbox(body=None, headers=None, hours_old=0, messages=1):
    assert tempfile.tempdir
    fd, name = tempfile.mkstemp()
    file = os.fdopen(fd, "w")
    for count in range(messages):
        msg = make_message(body=body, default_headers=headers, 
            hours_old=hours_old)
        file.write(msg)
    file.close()
    return name

def make_archive_and_plain_copy(archive_name):
    """Make an mbox archive of the given name like archivemail may have
    created it.  Also make an uncompressed copy of this archive and return its
    name."""
    copy_fd, copy_name = tempfile.mkstemp()
    copy_fp = os.fdopen(copy_fd, "w")
    if archivemail.options.no_compress:
        fd = os.open(archive_name, os.O_WRONLY|os.O_EXCL|os.O_CREAT)
        fp = os.fdopen(fd, "w")
    else:
        archive_name += ".gz"
        fd = os.open(archive_name, os.O_WRONLY|os.O_EXCL|os.O_CREAT)
        rawfp = os.fdopen(fd, "w")
        fp = gzip.GzipFile(fileobj=rawfp)
    for count in range(3):
        msg = make_message(hours_old=24*360)
        fp.write(msg)
        copy_fp.write(msg)
    fp.close()
    copy_fp.close()
    if not archivemail.options.no_compress:
        rawfp.close()
    return copy_name

def assertEqualContent(firstfile, secondfile, zippedfirst=False):
    """Verify that the two files exist and have identical content. If zippedfirst
    is True, assume that firstfile is gzip-compressed."""
    assert os.path.exists(firstfile)
    assert os.path.exists(secondfile)
    if zippedfirst:
        try:
            fp1 = gzip.GzipFile(firstfile, "r")
            fp2 = open(secondfile, "r")
            assert cmp_fileobj(fp1, fp2)
        finally:
            fp1.close()
            fp2.close()
    else:
        assert filecmp.cmp(firstfile, secondfile, shallow=0)

def cmp_fileobj(fp1, fp2):
    """Return if reading the fileobjects yields identical content."""
    bufsize = 8192
    while True:
        b1 = fp1.read(bufsize)
        b2 = fp2.read(bufsize)
        if b1 != b2:
            return False
        if not b1:
            return True

if __name__ == "__main__":
    unittest.main()
