"""
Submit jobs to slurm or torque, or with multiprocessing.

============================================================================

        AUTHOR: Michael D Dacre, mike.dacre@gmail.com
  ORGANIZATION: Stanford University
       LICENSE: MIT License, property of Stanford, use as you wish
       CREATED: 2016-44-20 23:03
 Last modified: 2016-04-08 14:36

   DESCRIPTION: Allows simple job submission with either torque, slurm, or
                with the multiprocessing module.
                To set the environement, set QUEUE to one of ['torque',
                'slurm', 'normal'], or run get_cluster_environment().
                To submit a job, run submit().

                All jobs write out a job file before submission, even though
                this is not necessary (or useful) with multiprocessing. In
                normal mode, this is a .cluster file, in slurm is is a
                .cluster.sbatch and a .cluster.script file, in torque it is a
                .cluster.qsub file.

                The name argument is required for submit, it is used to
                generate the STDOUT and STDERR files. Irrespective of mode
                the STDOUT file will be name.cluster.out and the STDERR file
                will be name.cluster.err.

                Note: `.cluster` is added to all names to make deletion less
                dangerous

                Dependency tracking is supported in torque or slurm mode,
                to use it pass a list of job ids to submit or submit_file with
                the `dependencies` keyword argument.

                To clean up cluster files, run clean(directory), if directory
                is not provided, the current directory is used.
                This will delete all files in that were generated by this
                script.

       CAUTION: The clean() function will delete **EVERY** file with
                extensions matching those in this file::
                    .cluster.err
                    .cluster.out
                    .cluster.sbatch & .cluster.script for slurm mode
                    .cluster.qsub for torque mode
                    .cluster for normal mode

============================================================================
"""
import os
import re
from time import sleep
from types import ModuleType
from textwrap import dedent
from subprocess import check_output, CalledProcessError
from multiprocessing import Pool, pool

###############################################################################
#                                Our functions                                #
###############################################################################

from . import run
from . import logme
from . import queue
from . import ClusterError

#########################
#  Which system to use  #
#########################

# Default is normal, change to 'slurm' or 'torque' as needed.
from . import QUEUE
from . import ALLOWED_QUEUES

#########################################################
#  The multiprocessing pool, only used in 'local' mode  #
#########################################################

from . import POOL

# Reset broken multithreading
# Some of the numpy C libraries can break multithreading, this command
# fixes the issue.
check_output("taskset -p 0xff %d &>/dev/null" % os.getpid(), shell=True)


###############################################################################
#                           Function Running Script                           #
###############################################################################


FUNC_RUNNER = """\
import pickle


def run_function(function_call, args=None):
    '''Run a function with args and return output.'''
    if not hasattr(function_call, '__call__'):
        raise FunctionError('{{}} is not a callable function.'.format(
            function_call))
    if args:
        if isinstance(args, (tuple, list)):
            out = function_call(*args)
        elif isinstance(args, dict):
            out = function_call(**args)
        else:
            out = function_call(args)
    else:
        out = function_call()
    return out

with open({pickle_file}, 'rb') as fin:
    function_call, args = pickle.load(fin)

try:
    out = run_function(function_call, args)
except Exception as e:
    out = e

with open({out_file}, 'wb') as fout:
    pickle.dump(out, fout)

"""

# Global Job Submission Arguments
KWARGS=dict(threads=None, cores=None, time=None, mem=None, partition=None,
            modules=None, dependencies=None)
ARGINFO="""\
:cores:        How many cores to run on or threads to use.
:dependencies: A list of dependencies for this job, must be either
                Job objects (required for normal mode) or job numbers.

Used for function calls::
:imports: A list of imports, if not provided, defaults to all current
            imports, which may not work if you use complex imports.
            The list can include the import call, or just be a name, e.g
            ['from os import path', 'sys']

Used for torque and slurm::
:time:      The time to run for in HH:MM:SS.
:mem:       Memory to use in MB.
:partition: Partition/queue to run on, default 'normal'.
:modules:   Modules to load with the 'module load' command.
"""

###############################################################################
#                                The Job Class                                #
###############################################################################


class Job(object):

    """Information about a single job on the cluster.

    Holds information about submit time, number of cores, the job script,
    and more.

    submit() will submit the job if it is ready
    wait()   will block until the job is done
    get()    will block until the job is done and then unpickle a stored
             output (if defined) and return the contents
    clean()  will delete any files created by this object

    Printing the class will display detailed job information.

    Both wait() and get() will update the queue every two seconds and add
    queue information to the job as they go.

    If the job disappears from the queue with no information, it will be listed
    as 'complete'.

    All jobs have a .submission attribute, which is a Script object containing
    the submission script for the job and the file name, plus a 'written' bool
    that checks if the file exists.

    In addition, SLURM jobs have a .exec_script attribute, which is a Script
    object containing the shell command to run. This difference is due to the
    fact that some SLURM systems execute multiple lines of the submission file
    at the same time.

    Finally, if the job command is a function, this object will also contain a
    .function attribute, which contains the script to run the function.

    """

    # Scripts
    submission   = None
    exec_script  = None
    function     = None

    # Dependencies
    dependencies = None

    def write(self):
        """Write all scripts."""
        submission.write_file()
        if exec_script:
            exec_script.write_file()
        if function:
            function.write_file()

    def __init__(name, command, args=None, path=None, **KWARGS):
        """Create a job object will submission information.

        Used in all modes::
        :name:         The name of the job.
        :command:      The command or function to execute.
        :path:         Where to create the script, if None, current dir used.
        :args:         Optional arguments to add to command, particularly
                       useful for functions.
        {arginfo}
        """.format(ARGINFO)
        # Sanitize arguments
        name    = str(name)
        cores   = cores if cores else 1  # In case cores are passed as None
        modules = [modules] if isinstance(modules, str) else modules
        usedir  = os.path.abspath(path) if path else os.path.abspath('.')

        # Make sure args are a tuple or dictionary
        if args:
            if not isinstance(args, (tuple, dict)):
                if isinstance(args, list, set):
                    args = tuple(args)
                else:
                    args = (args,)

        # Cores
        self.cores = cores

        # Set dependencies
        if dependencies:
            if isinstance(dependencies, 'str'):
                if not dependencies.isdigit():
                    raise ClusterError('Dependencies must be number or list')
                else:
                    dependencies = [int(dependencies)]
            elif isinstance(dependencies, (int, job)):
                dependencies = [dependencies]
            elif not isinstance(dependencies, (tuple, list)):
                raise ClusterError('Dependencies must be number or list')
            for dependency in dependencies:
                if isinstance(dependency, str):
                    dependency  = int(dependency)
                if not isinstance(dependency, (int, Job)):
                    raise ClusterError('Dependencies must be number or list')

        # Make functions run remotely
        if hasattr(command, '__call__'):
            self.function = Function(
                file_name=os.path.join(usedir, name + 'func.py'),
                function=command, args=args)
            command = 'python{} {}'.format(sys.version[0],
                                           self.function.file_name)
            args = None

        # Collapse args into command
        command = command + ' '.join(args) if args else command

        # Build execution wrapper with modules
        precmd  = ''
        if modules:
            for module in modules:
                precmd += 'module load {}\n'.format(module)
        precmd += dedent("""\
            cd {}
            date +'%d-%H:%M:%S'
            echo "Running {}"
            """.format(usedir, name))
        pstcmd = dedent("""\
            exitcode=$?
            echo Done
            date +'%d-%H:%M:%S'
            if [[ $exitcode != 0 ]]; then
                echo Exited with code: $? >&2
            fi
            """)

        # Create queue-dependent scripts
        sub_script = []
        if QUEUE == 'slurm':
            scrpt = os.path.join(usedir, '{}.cluster.sbatch'.format(name))
            sub_script.append('#!/bin/bash')
            if partition:
                sub_script.append('#SBATCH -p {}'.format(partition))
            sub_script.append('#SBATCH --ntasks 1')
            sub_script.append('#SBATCH --cpus-per-task {}'.format(cores))
            if time:
                sub_script.append('#SBATCH --time={}'.format(time))
            if mem:
                sub_script.append('#SBATCH --mem={}'.format(mem))
            sub_script.append('#SBATCH -o {}.cluster.out'.format(name))
            sub_script.append('#SBATCH -e {}.cluster.err'.format(name))
            sub_script.append('cd {}'.format(usedir))
            sub_script.append('srun bash {}.script'.format(
                os.path.join(usedir, name)))
            exe_scrpt  = os.path.join(usedir, name + '.script')
            exe_script = []
            exe_script.append('#!/bin/bash')
            exe_script.append('mkdir -p $LOCAL_SCRATCH')
            exe_script.append(precmd)
            exe_script.append(command + '\n')
            exe_script.append(pstcmd)
        elif QUEUE == 'torque':
            scrpt = os.path.join(usedir, '{}.cluster.qsub'.format(name))
            sub_script.append('#!/bin/bash')
            if partition:
                sub_script.append('#PBS -q {}'.format(partition))
            sub_script.append('#PBS -l nodes=1:ppn={}'.format(cores))
            if time:
                sub_script.append('#PBS -l walltime={}'.format(time))
            if mem:
                sub_script.append('#PBS mem={}MB'.format(mem))
            sub_script.append('#PBS -o {}.cluster.out'.format(name))
            sub_script.append('#PBS -e {}.cluster.err\n'.format(name))
            sub_script.append('mkdir -p $LOCAL_SCRATCH')
            sub_script.append(precmd)
            sub_script.append(command + '')
            sub_script.append(pstcmd)
        elif QUEUE == 'normal':
            scrpt = os.path.join(usedir, '{}.cluster'.format(name))
            sub_script.append('#!/bin/bash\n')
            sub_script.append(precmd)
            sub_script.append(command + '\n')
            sub_script.append(pstcmd)

        # Create the Script objects
        self.submission = Script(script='\n'.join(sub_script),
                                 file_name=scrpt)
        if exe_scrpt:
            self.exec_script = Script(script='\n'.join(exe_script),
                                      file_name=exe_scrpt)


class Script(object):

    """A script string plus a file name."""

    written = False

    def __init__(self, file_name, script):
        """Initialize the script and file name."""
        self.script    = script
        self.file_name = os.path(abspath(file_name))

    def write_file(self, overwrite=False):
        """Write the script file."""
        if overwrite or not os.path.exists(self.file_name):
            with open(self.file_name, 'w') as fout:
                fout.write(self.script + '\n')
            self.written = True
            return self.file_name
        else:
            return None

    def __getattr__(self, attr):
        """Make sure boolean is up to date."""
        if attr == 'exists':
            return os.path.exists(self.file_name)

    def __repr__(self):
        """Display simple info."""
        return "Script<{}(exists: {}; written: {})>".format(
            self.file_name, self.exists, self.written)

    def __str__(self):
        """Print the script."""
        return repr(self) + '::\n\n' + self.script + '\n'


class Function(Script):

    """A special Script used to run a function."""

    def __init__(self, file_name, function, args=None, imports=None,
                 pickle_file=None, outfile=None):
        """Create a function wrapper.

        :function:    Function handle.
        :args:        Arguments to the function as a tuple.
        :imports:     A list of imports, if not provided, defaults to all current
                    imports, which may not work if you use complex imports.
                    The list can include the import call, or just be a name, e.g
                    ['from os import path', 'sys']
        :pickle_file: The file to hold the function.
        :outfile:     The file to hold the output.
        """
        script = '#!/usr/bin/env python{}\n'.format(sys.version[0])
        if imports:
            if not isinstance(imports, (list, tuple)):
                imports = [imports]
        else:
            imports = []
            for name, module in globals().items():
                if isintsance(module, ModuleType):
                    imports.append(module.__name__)
            imports = list(set(imports))

        for imp in imports:
            if imp.startswith('import') or imp.startswith('from'):
                imp = imp
            else:
                imp = 'import {}\n'.format(imp)
            script += imp

        # Set file names
        self.pickle_file = pickle_file if pickle_file else file_name + '.pickle.in'
        self.outfile     = outfile if outfile else file_name + '.pickle.out'

        # Create script text
        script += '\n\n' + FUNC_RUNNER.format(pickle_file=self.pickle_file,
                                            out_file=self.outfile)

        super(Function, self).__init__(file_name, script)


###############################################################################
#                            Submission Functions                             #
###############################################################################


def submit(name, command, args=None, path=None, **KWARGS):
    """Submit a script to the cluster.

    Used in all modes::
    :name:      The name of the job.
    :command:   The command or function to execute.
    :path:         Where to create the script, if None, current dir used.
    :args:         Optional arguments to add to command, particularly
                    useful for functions.

    {arginfo}

    Returns:
        Job object
    """.format(ARGINFO)
    queue.check_queue()  # Make sure the QUEUE is usable

    cores = cores if cores else 1

    if QUEUE == 'slurm' or QUEUE == 'torque':
        return submit_file(make_job_file(command, name, time, cores,
                                         mem, partition, modules, path),
                           dependencies=dependencies)
    elif QUEUE == 'normal':
        return submit_file(make_job_file(command, name), name=name,
                           threads=cores)


def submit_file(script_file, name=None, dependencies=None, threads=None):
    """Submit a job file to the cluster.

    If QUEUE is torque, qsub is used; if QUEUE is slurm, sbatch is used;
    if QUEUE is normal, the file is executed with subprocess.

    :dependencies: A job number or list of job numbers.
                   In slurm: `--dependency=afterok:` is used
                   For torque: `-W depend=afterok:` is used

    :threads:      Total number of threads to use at a time, defaults to all.
                   ONLY USED IN NORMAL MODE

    :name:         The name of the job, only used in normal mode.

    :returns:      job number for torque or slurm
                   multiprocessing job object for normal mode
    """
    queue.check_queue()  # Make sure the QUEUE is usable

    # Sanitize arguments
    name = str(name)

    # Check dependencies
    if dependencies:
        if isinstance(dependencies, (str, int)):
            dependencies = [dependencies]
        if not isinstance(dependencies, (list, tuple)):
            raise Exception('dependencies must be a list, int, or string.')
        dependencies = [str(i) for i in dependencies]

    if QUEUE == 'slurm':
        if dependencies:
            dependencies = '--dependency=afterok:{}'.format(
                ':'.join([str(d) for d in dependencies]))
            args = ['sbatch', dependencies, script_file]
        else:
            args = ['sbatch', script_file]
        # Try to submit job 5 times
        count = 0
        while True:
            try:
                job = int(check_output(args).decode().rstrip().split(' ')[-1])
            except CalledProcessError:
                if count == 5:
                    raise
                count += 1
                sleep(1)
                continue
            break
        return job
    elif QUEUE == 'torque':
        if dependencies:
            dependencies = '-W depend={}'.format(
                ','.join(['afterok:' + d for d in dependencies]))
            args = ['qsub', dependencies, script_file]
        else:
            args = ['qsub', script_file]
        # Try to submit job 5 times
        count = 0
        while True:
            try:
                job = int(check_output(args).decode().rstrip().split('.')[0])
            except CalledProcessError:
                if count == 5:
                    raise
                count += 1
                sleep(1)
                continue
            break
        return job
    elif QUEUE == 'normal':
        global POOL
        if not POOL:
            POOL = Pool(threads) if threads else Pool()
        command = 'bash {}'.format(script_file)
        args = dict(stdout=name + '.cluster.out', stderr=name + '.cluster.err')
        return POOL.apply_async(run.cmd, (command,), args)


#########################
#  Job file generation  #
#########################

def make_job(name, command, args=None, path=None, **KWARGS):
    """Make a job file compatible with the chosen cluster.

    If mode is normal, this is just a simple shell script.

     Used in all modes::
    :name:      The name of the job.
    :command:   The command or function to execute.
    :path:         Where to create the script, if None, current dir used.
    :args:         Optional arguments to add to command, particularly
                    useful for functions.

    {arginfo}

    Returns:
        A job object
    """.format(ARGINFO)

    queue.check_queue()  # Make sure the QUEUE is usable

    job = Job(name, command, args=args, path=path, cores=cores, time=time,
              mem=mem, partition=partition, modules=modules,
              dependencies=dependencies)

    # Return the path to the script
    return job


def make_job_file(name, command, args=None, path=None, **KWARGS):
    """Make a job file compatible with the chosen cluster.

    If mode is normal, this is just a simple shell script.

     Used in all modes::
    :name:      The name of the job.
    :command:   The command or function to execute.
    :path:         Where to create the script, if None, current dir used.
    :args:         Optional arguments to add to command, particularly
                    useful for functions.

    {arginfo}

    Returns:
        Path to job script
    """.format(ARGINFO)

    queue.check_queue()  # Make sure the QUEUE is usable

    job = Job(name, command, args=args, path=path, cores=cores, time=time,
              mem=mem, partition=partition, modules=modules,
              dependencies=dependencies)

    job = job.write()

    # Return the path to the script
    return job.submission


##############
#  Cleaning  #
##############


def clean(directory='.'):
    """Delete all files made by this module in directory.

    CAUTION: The clean() function will delete **EVERY** file with
             extensions matching those in this file::
                 .cluster.err
                 .cluster.out
                 .cluster.sbatch & .cluster.script for slurm mode
                 .cluster.qsub for torque mode
                 .cluster for normal mode

    :directory: The directory to run in, defaults to the current directory.
    :returns:   A set of deleted files
    """
    queue.check_queue()  # Make sure the QUEUE is usable

    extensions = ['.cluster.err', '.cluster.out']
    if QUEUE == 'normal':
        extensions.append('.cluster')
    elif QUEUE == 'slurm':
        extensions = extensions + ['.cluster.sbatch', '.cluster.script']
    elif QUEUE == 'torque':
        extensions.append('.cluster.qsub')

    files = [i for i in os.listdir(os.path.abspath(directory))
             if os.path.isfile(i)]

    if not files:
        logme.log('No files found.', 'debug')
        return []

    deleted = []
    for f in files:
        for extension in extensions:
            if f.endswith(extension):
                os.remove(f)
                deleted.append(f)

    return deleted
