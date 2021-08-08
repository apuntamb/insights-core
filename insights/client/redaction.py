'''
Functions for performing redaction and obfuscation on a
file, directory, or InsightsArchive
'''
from __future__ import absolute_import
import os
import errno
import json
import logging
import copy
import glob
import six
import shlex
import re
import tempfile
from subprocess import Popen, PIPE, STDOUT
from .constants import InsightsConstants as constants

APP_NAME = constants.app_name
logger = logging.getLogger(__name__)
# python 2.7
SOSCLEANER_LOGGER = logging.getLogger('soscleaner')
SOSCLEANER_LOGGER.setLevel(logging.ERROR)
# python 2.6
SOSCLEANER_LOGGER = logging.getLogger('insights-client.soscleaner')
SOSCLEANER_LOGGER.setLevel(logging.ERROR)

# some files should not be redacted because they contain
#   IDs or data required for service functionality
SKIPLIST = [
    'etc/insights-client/machine-id',
    'etc/machine-id',
    'insights_commands/subscription-manager_identity',
    'display_name',
    'blacklist_report',
    'tags.json',
    'branch_info',  # TODO redact this one
    'version_info',
    'egg_release'
]

def _process_content_redaction(filepath, exclude, regex=False):
    '''
    Redact content from a file, based on
    /etc/insights-client/.exp.sed and and the contents of "exclude"

    filepath    file to modify
    exclude     list of strings to redact
    regex       whether exclude is a list of regular expressions

    Returns the file contents with the specified data removed
    '''
    logger.debug('Processing %s...', filepath)

    # password removal
    sedcmd = Popen(['sed', '-rf', constants.default_sed_file, filepath], stdout=PIPE)
    # patterns removal
    if exclude:
        exclude_file = NamedTemporaryFile()
        exclude_file.write("\n".join(exclude).encode('utf-8'))
        exclude_file.flush()
        if regex:
            flag = '-E'
        else:
            flag = '-F'
        grepcmd = Popen(['grep', '-v', flag, '-f', exclude_file.name], stdin=sedcmd.stdout, stdout=PIPE)
        sedcmd.stdout.close()
        stdout, stderr = grepcmd.communicate()
        logger.debug('Process status: %s', grepcmd.returncode)
    else:
        stdout, stderr = sedcmd.communicate()
        logger.debug('Process status: %s', sedcmd.returncode)
    logger.debug('Process stderr: %s', stderr)
    return stdout


def redact_file(original, redacted):
    '''
    '''
    pass

def redact_insights_archive(config, archive, rm_conf, topdir=None):
    '''
    Perform data redaction (password sed command and patterns),
    on an InsightsArchive.

    :param config: an InsightsConfig object
    :param archive: the InsightsArchive to be redacted
    :param rm_conf: denylist configuration,
        typically loaded from InsightsUploadConf.get_rm_conf()
    :param topdir: optional directory within the top level
        of archive.archive_dir to treat as the "root" of
        the redaction process

    :returns: an InsightsArchive with redaction applied

    :raises RuntimeError: when the InsightsArchive path is invalid
    '''
    pass

def redact_directory(config, directory, rm_conf):
    '''
    Perform data redaction (password sed command and patterns),
    on a directory.

    :param config: an InsightsConfig object
    :param directory: the directory to be redacted
    :param rm_conf: denylist configuration,
        typically loaded from InsightsUploadConf.get_rm_conf()

    :returns: filepath string of a new directory with redaction applied

    :raises RuntimeError: when the InsightsArchive path is invalid
    '''
    logger.debug('Running content redaction...')

    redacted_directory = tempfile.mkdtemp(prefix='/var/tmp/')

    if rm_conf is None:
        rm_conf = {}
    patterns = None
    regex = False
    if rm_conf:
        try:
            patterns = rm_conf['patterns']
            if isinstance(patterns, dict) and patterns['regex']:
                # if "patterns" is a dict containing a non-empty "regex" list
                logger.debug('Using regular expression matching for patterns.')
                patterns = patterns['regex']
                regex = True
            logger.warn("WARNING: Skipping patterns defined in blacklist configuration")
        except LookupError:
            # either "patterns" was undefined in rm conf, or
            #   "regex" was undefined in "patterns"
            patterns = None
    if not patterns:
        logger.debug('Patterns section of blacklist configuration is empty.')

    for dirpath, dirnames, filenames in os.walk(directory):
        for f in filenames:
            # original path to file f
            src_file = os.path.join(dirpath, f)
            # path to file f relative to the source directory
            rel_file = os.path.relpath(src_file, directory)
            # path to directory containing file f, relative to the source directory
            rel_dir = os.path.relpath(dirpath, directory)
            # path to directory containing file f, relative to the destination redacted directory
            dst_dir = os.path.join(redacted_directory, rel_dir)
            # path to file f relative to the destination redacted directory
            dst_file = os.path.join(redacted_directory, rel_file)
            try:
                # make the destination subdirectories
                os.makedirs(dst_dir)
            except OSError:
                # exists
                pass
            redacted_contents = _process_content_redaction(src_file, patterns, regex)
            with open(dst_file, 'wb') as dst:
                dst.write(redacted_contents)

def obfuscate_file():
    pass

def obfuscate_directory():
    pass

def obfuscate_insights_archive():
    pass

if __name__ == "__main__":
    from insights.client.config import InsightsConfig
    conf = InsightsConfig()
    rm_conf = {}
    tmpdir = "/var/tmp/redaction-test"
    redact_directory(conf, tmpdir, rm_conf)
