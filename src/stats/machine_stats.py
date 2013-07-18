#!/usr/bin/python2.7
#
# Copyright 2013 Google Inc. All Rights Reserved.

"""Machine Stats.

The model of the Machine Stats, and various helper functions.
"""


import datetime
import logging

from google.appengine.api import app_identity
from google.appengine.ext import ndb

from server import admin_user


# The number of hours that have to pass before a machine is considered dead.
MACHINE_DEATH_TIMEOUT = datetime.timedelta(hours=3)

# The amount of time that needs to pass before the last_seen field of
# MachineStats will update for a given machine, to prevent too many puts.
MACHINE_UPDATE_TIME = datetime.timedelta(hours=1)

# The message to use for each dead machine.
_INDIVIDUAL_DEAD_MACHINE_MESSAGE = (
    'Machine %(machine_id)s(%(machine_tag)s) was last seen %(last_seen)s and '
    'is assumed to be dead.')

# The message body of the dead machine message to send admins.
_DEAD_MACHINE_MESSAGE_BODY = """Hello,

The following registered machines haven't been active in %(timeout)s.

%(death_summary)s

Please revive the machines or remove them from the list of active machines.
"""

# The dict of acceptable keys to sort MachineStats by, with the key as the key
# and the value as the human readable name.
ACCEPTABLE_SORTS = {
    'dimensions': 'Dimensions',
    'last_seen': 'Last Seen',
    'machine_id': 'Machine ID',
    'tag': 'Tag',
}


class MachineStats(ndb.Model):
  """A machine's stats."""
  # The tag of the machine polling.
  tag = ndb.StringProperty(default='')

  # The dimensions of the machine polling.
  dimensions = ndb.StringProperty(default='')

  # The last day the machine queried for work.
  last_seen = ndb.DateTimeProperty(auto_now_add=True, required=True)

  # The machine id, which is also the model's key.
  machine_id = ndb.ComputedProperty(lambda self: self.key.string_id())


def FindDeadMachines():
  """Find all dead machines.

  Returns:
    A list of the dead machines.
  """
  dead_machine_cutoff = (datetime.datetime.now() - MACHINE_DEATH_TIMEOUT)

  return list(MachineStats.gql('WHERE last_seen < :1', dead_machine_cutoff))


def NotifyAdminsOfDeadMachines(dead_machines):
  """Notify the admins of the dead_machines detected.

  Args:
    dead_machines: The list of the currently dead machines.

  Returns:
    True if the email was successfully sent.
  """
  death_summary = []
  for machine in dead_machines:
    death_summary.append(
        _INDIVIDUAL_DEAD_MACHINE_MESSAGE % {'machine_id': machine.machine_id,
                                            'machine_tag': machine.tag,
                                            'last_seen': machine.last_seen})

  subject = 'Dead Machines Found on %s' % app_identity.get_application_id()
  body = _DEAD_MACHINE_MESSAGE_BODY % {
      'timeout': MACHINE_DEATH_TIMEOUT,
      'death_summary': '\n'.join(death_summary)}

  return admin_user.EmailAdmins(subject, body)


def RecordMachineQueriedForWork(machine_id, dimensions_str, machine_tag):
  """Records when a machine has queried for work.

  Args:
    machine_id: The machine id of the machine.
    dimensions_str: The string representation of the machines dimensions.
    machine_tag: The tag identifier of the machine.
  """
  machine_stats = MachineStats.get_or_insert(machine_id)

  if (machine_stats.dimensions != dimensions_str or
      machine_stats.last_seen + MACHINE_UPDATE_TIME < datetime.datetime.now() or
      machine_stats.tag != machine_tag):
    machine_stats.dimensions = dimensions_str
    machine_stats.last_seen = datetime.datetime.now()
    machine_stats.tag = machine_tag
    machine_stats.put()


def DeleteMachineStats(key):
  """Delete the machine assignment referenced to by the given key.

  Args:
    key: The key of the machine assignment to delete.

  Returns:
    True if the key was valid and machine assignment was successfully deleted.
  """
  try:
    key = ndb.Key(MachineStats, key)
    if not key.get():
      logging.error('No MachineStats has key: %s', str(key))
      return False
  except Exception:  # pylint: disable=broad-except
    # All exceptions must be caught because some exceptions can only be caught
    # this way. See this bug report for more details:
    # https://code.google.com/p/appengine-ndb-experiment/issues/detail?id=143
    logging.error('Invalid MachineStats key given, %s', str(key))
    return False

  key.delete()
  return True


def GetAllMachines(sort_by='machine_id'):
  """Get the list of whitelisted machines.

  Args:
    sort_by: The string of the attribute to sort the machines by.

  Returns:
    An iterator of all machines whitelisted.
  """
  # If we receive an invalid sort_by parameter, just default to machine_id.
  if sort_by not in ACCEPTABLE_SORTS:
    sort_by = 'machine_id'

  return (machine for machine in MachineStats.gql('ORDER BY %s' % sort_by))


def GetMachineTag(machine_id):
  """Get the tag for a given machine id.

  Args:
    machine_id: The machine id to find the tag for

  Returns:
    The machine's tag, or None if the machine id isn't used.
  """
  machine = MachineStats.get_by_id(machine_id) if machine_id else None

  return machine.tag if machine else 'Unknown'
