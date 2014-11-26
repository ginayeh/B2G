#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import json
import operator
from sets import Set
from os import listdir
from os.path import isfile
from os import environ
import subprocess


tasks = {}
sorted_tasks = {}
unrun_tasks = set([])
unfinished_tasks = set([])
processes = {}
threads = {}
_unwanted_dup_names = set(['vtable for mozilla::ipc::DoWorkRunnable',
                           'vtable for nsTimerEvent'])

class ParseError(Exception):
  def __init__(self, error_msg):
    self.msg = error_msg
    self.log = ''

class BaseObject(object):
  def __init__(self, id, name=''):
    self.id = id
    self.name = name

  def pretty_dict(self):
    return {key: value for key, value in self.__dict__.iteritems() if not key.startswith('_')}

class Process(BaseObject):
  def __init__(self, id, name):
    super(Process, self).__init__(id, name)
    self._mem_offset = 0
    self._start = 0

class Thread(BaseObject):
  def __init__(self, id, name):
    super(Thread, self).__init__(id, name)

class Label(object):
  def __init__(self, timestamp, label):
    super(Label, self).__init__()
    self.timestamp = timestamp
    self.label = label

  def pretty_dict(self):
    return self.__dict__

class Task(BaseObject):
  def __init__(self, id):
    super(Task, self).__init__(id)

    self.sourceEventId = 0
    self.sourceEventType = None
    self.processId = 0
    self.threadId = 0
    self.parentTaskId = 0
    self.labels = []
    self._vptr = 0

    # Timestamp information
    self.dispatch = 0
    self.begin = 0
    self.end = 0
    self.latency = 0
    self.executionTime = 0

  def add_label(self, timestamp, label):
    self.labels.append(Label(timestamp, label))

def find_char_and_split(string, char=' ', num_split=-1):
  """
    Find the delimiter first and then split the string with the delimiter.

    returns:
      A list of the words in the string after spliting the string.
      None when failed to find the delimiter in the string.
  """
  if string.find(char) == -1:
    raise ParseError('Extract error: no \'{}\''.format(char))

  if string.count(char) < num_split:
    print num_split, string
    raise ParseError('Extract error: no enough \'{}\''.format(char))


  if num_split == -1:
    return string.split(char)
  else:
    return string.split(char, num_split)

def verify_info(info):
  """
    Verify task information based on log type.

    info: A list of task properties.
      DISPATCH: [0 taskId dispatch sourceEventId sourceEventType parentTaskId]
      BEGIN:    [1 taskId begin processId threadId]
      END:      [2 taskId end]
      LABEL:    [3 taskId timestamp, label]
      VPTR:     [4 taskId vptr]

    returns:
      True when verification passed.
      False when verification failed.
  """
  log_type = info[0]
  if not log_type in range(0, 5):
    raise ParseError('Verify error: invalid log type \'{}\''.format(log_type))

  if any(((log_type == 0) and (len(info) != 6),
          (log_type == 1) and (len(info) != 5),
          (log_type == 2) and (len(info) != 3),
          (log_type == 3) and (len(info) != 4),
          (log_type == 4) and (len(info) != 3))):
     raise ParseError('Verify error: incomplete information')

def set_task_info(info, process_id):
  """
    Set task properties based on log type.

    info: A list of verified task properties.
    process_id: Default value of Task.processId.
  """
  log_type = info[0]
  task_id = info[1]
  if task_id not in tasks:
    tasks[task_id] = Task(int(task_id))
    tasks[task_id].processId = int(process_id)
    tasks[task_id].processName = processes[process_id].name

  if log_type == 4:
    tasks[task_id]._vptr = int(info[2], 16)
    return

  timestamp = int(info[2])
  if log_type == 0:
    tasks[task_id].dispatch = timestamp
    if tasks[task_id].begin > 0:
      tasks[task_id].latency = tasks[task_id].begin - tasks[task_id].dispatch
    if tasks[task_id].end > 0:
      tasks[task_id].executionTime = tasks[task_id].end - tasks[task_id].begin
    tasks[task_id].sourceEventId = int(info[3])
    tasks[task_id].sourceEventType = int(info[4])
    tasks[task_id].parentTaskId = int(info[5])
  elif log_type == 1:
    tasks[task_id].begin = timestamp
    if tasks[task_id].dispatch > 0:
      tasks[task_id].latency = tasks[task_id].begin - tasks[task_id].dispatch

    thread_id = int(info[4])
    # For threads which aren't registered, they may have no name.
    if info[4] not in threads:
      threads[info[4]] = Thread(thread_id, '')
    tasks[task_id].threadId = thread_id
    tasks[task_id].threadName = threads[info[4]].name
  elif log_type == 2:
    tasks[task_id].end = timestamp
    if tasks[task_id].begin > 0:
      tasks[task_id].executionTime = tasks[task_id].end - tasks[task_id].begin
  elif log_type == 3:
    tasks[task_id].add_label(timestamp, info[3])

def parse_log(log, process_id):
  """
    Parse log line by line and verify the parsing results based on the log type.
    Then, set up task information with the verified parsing results.
  """
  for line in log:
    info = None
    try:
      # Get log type
      [log_type, remain] = find_char_and_split(line.strip(), ' ', 1)

      # log_type:
      #   0 - DISPATCH. Ex. 0 taskId dispatch sourceEventId sourceEventType parentTaskId
      #   1 - BEGIN.    Ex. 1 taskId begin processId threadId
      #   2 - END.      Ex. 2 taskId end
      #   3 - LABEL.    Ex. 3 taskId timestamp "label"
      #   4 - VPTR.     Ex. 4 address
      if log_type == '3':
        [task_id, timestamp, remain] = find_char_and_split(remain, ' ', 2)
        info = [int(log_type), task_id, timestamp, remain.replace("\"", "")]
      else:
        tokens = find_char_and_split(remain)
        info = [int(log_type)] + tokens

      verify_info(info)
    except ParseError as error:
      error.log = line.strip()
      raise

    set_task_info(info, process_id)

def output_json(output_name, begin_time, end_time):
  """
    Write tasks out in JSON format.

    output_name: Output filename.
    begin_time: the min of all timestamps.
    end_time: the max of all timestamps.
  """
  output_file = open(output_name, 'w')
  output_file.write('{\"begin\": %d, \"end\": %d, \"processes\": '
                    % (begin_time, end_time))
  output_file.write(json.dumps(processes.values(), default=lambda o:o.pretty_dict(),
                    indent=4))
  output_file.write(', \"threads\": ')
  output_file.write(json.dumps(threads.values(), default=lambda o:o.pretty_dict(),
                    indent=4))
  output_file.write(', \"tasks\": ')
  output_file.write(json.dumps(sorted_tasks, default=lambda o: o.pretty_dict(),
                               indent=4))

  output_file.write('}')
  output_file.close()

def binary_search(address, x, lo=0, hi=None):
  if hi is None:
    hi = len(address)
    while lo < hi:
      if (hi - lo == 1):
        return address[lo][1]
      mid = (lo + hi) / 2
      midval = address[mid][0]
      if (midval < x):
        lo = mid
      elif (midval > x):
        hi = mid
      else:
        return address[mid][1]

def retrieve_task_name(nm_path, libxul_path):
  """Retrieve symbol from libxul.so with memory maps."""
  p1 = subprocess.Popen([nm_path, '-a', libxul_path], stdout=subprocess.PIPE)
  p2 = subprocess.Popen(['grep', '_Z'], stdin=p1.stdout, stdout=subprocess.PIPE)
  p3 = subprocess.Popen(['c++filt'], stdin=p2.stdout, stdout=subprocess.PIPE)
  p4 = subprocess.Popen(['sort'], stdin=p3.stdout, stdout=subprocess.PIPE)
  p1.stdout.close()
  p2.stdout.close()
  p3.stdout.close()
  output = p4.communicate()[0]
  all_symbols = find_char_and_split(output, '\n')

  address = []
  for line in all_symbols:
    if len(line) == 0:
      continue

    try:
      tokens = find_char_and_split(line, ' ', 2)
    except ParseError:
      raise

    if len(tokens[0]) == 0:
      continue

    address.append((int(tokens[0], 16), tokens[2].strip()))

  # Get name for each task
  for task_id, task_obj in tasks.iteritems():
    if not (task_obj._vptr and task_obj.processId and
      processes[str(task_obj.processId)]._mem_offset):
      continue

    offset = task_obj._vptr - processes[str(task_obj.processId)]._mem_offset

    task_obj.name = binary_search(address, offset)

def read_log(input_folder):
  """
    Read all log files generated by built-in profiler. Process name is extracted
    from the filename. Task information and thread information are recorded in
    the json file.

    For example,
      {
        "tasktracer": {
          "data": [{log1}, {log2}, {log3}, ...],
          "threads": [{t1}, {t2}, {t3}, ...],
          "start": ...,
        }
      }
  """
  for filename in listdir(input_folder):
    if not filename.startswith('profile_') or not filename.endswith(".txt"):
      continue

    print 'Parsing {}...'.format(filename)
    # Set up process info. Example filename: profile_3810_b2g.txt
    [name, ext] = find_char_and_split(filename, '.', 1)
    [prefix, process_id, process_name] = find_char_and_split(name, "_")
    processes[process_id] = Process(int(process_id), process_name)

    # Load json file and get tasktracer log.
    with open(input_folder + '/' + filename, 'r') as json_file:
      json_data = json.load(json_file)
      task_info = json_data["tasktracer"]["data"]
      thread_info = json_data["tasktracer"]["threads"]
      processes[process_id]._start = json_data["tasktracer"]["start"]

    # Set up thread info.
    for t in thread_info:
      thread_id = t["tid"]
      thread_name = t["name"]
      threads[str(thread_id)] = Thread(thread_id, thread_name)

    parse_log(task_info, process_id)

def read_mmap(mmap_path):
  """
    Read all memory mmaps and set Process._mem_offset for retrieving task name
    later.
  """
  for filename in listdir(mmap_path):
    [prefix, process_id] = find_char_and_split(filename, '_')
    if str(process_id) not in processes:
      continue

    with open(mmap_path + '/' + filename, 'r') as mmap_file:
      mmap_data = mmap_file.readlines()

    for line in mmap_data:
      if 'libxul.so' in line:
        [mem_offset, others] = find_char_and_split(line, '-', 1);
        processes[str(process_id)]._mem_offset = int(mem_offset, 16)
        break

def remove_dup_tasks():
  """
    Remove tasks that is a wrapper or dequeuer of another task or tasks.

    This would reduce annonying nested task phenomenons.
  """
  removing_list = [task_id
                   for task_id, task_obj in tasks.iteritems()
                   if task_obj.name in _unwanted_dup_names]
  for task_id in removing_list:
    tasks.pop(task_id)
    pass
  pass

def replace_with_relative_time(begin):
  """
    Convert dispatch/begin/end to relative timestamp.
    Return max timestamp.
  """
  max_time = 0
  global unrun_tasks, unfinished_tasks
  for task in tasks.itervalues():
    if task.dispatch != 0:
      task.dispatch = task.dispatch - begin

    if task.begin != 0:
      task.begin = task.begin - begin
    else:
      unrun_tasks.add(task.id)

    if task.end != 0:
      task.end = task.end - begin
    else:
      unfinished_tasks.add(task.id)

    if task.dispatch > max_time:
      max_time = task.dispatch
    if task.begin > max_time:
      max_time = task.begin
    if task.end > max_time:
      max_time = task.end

  return max_time

def main():
  input_log_path = environ['ANDROID_BUILD_TOP']
  mmap_path = '/tmp/mmap'
  libxul_path = '{}/dist/bin/libxul.so'.format(environ['GECKO_OBJDIR'])
  nm_path = '{}/arm-linux-androideabi-nm'.format(environ['ANDROID_EABI_TOOLCHAIN'])

  print '====================================================='
  print 'Input log path:', input_log_path
  print 'Input mmap path:', mmap_path
  print 'libxul.so path:', libxul_path
  print 'nm path:', nm_path
  print 'Output: task_tracer_data.json'
  print '====================================================='

  try:
    read_log(input_log_path)
    print '\n{} tasks has been creates successfully.'.format(len(tasks))

    print '\nRetriving task name...'
    read_mmap(mmap_path)
    retrieve_task_name(nm_path, libxul_path)

    # Removing duplicated tasks.
    remove_dup_tasks()
  except ParseError as error:
    print error.msg
    if error.log:
      print '@line: \'{}\''.format(error.log)
    sys.exit()
  if len(tasks) == 0:
    sys.exit()

  process_start_min = None
  for p in processes.itervalues():
    if process_start_min == None:
      process_start_min = p._start
    elif p._start < process_start_min:
      process_start_min = p._start

  # Replacing with relative timestamp.
  begin_time = 0
  end_time = replace_with_relative_time(process_start_min)

  # Filling incomplete tasks with end time.
  for task_id in unrun_tasks:
    tasks[str(task_id)].begin = end_time
    tasks[str(task_id)].end = end_time

  for task_id in unfinished_tasks:
    tasks[str(task_id)].end = end_time

  # Sort tasks by dispatch time.
  global sorted_tasks
  sorted_tasks = sorted(tasks.values(), key=operator.attrgetter('dispatch'))

  output_json('tasktracer_data.json', begin_time, end_time)

  print '\nDone! {} tasks has been written to task_tracer_data.json successfully.'.format(len(tasks))

if __name__ == '__main__':
  sys.exit(main())

