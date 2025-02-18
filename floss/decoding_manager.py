import logging
from collections import namedtuple

from enum import Enum
import viv_utils
import viv_utils.emulator_drivers
import envi.memory


floss_logger = logging.getLogger("floss")

# A DecodedString stores the decoded string and meta data about it:
# va: va of string in memory, s: decoded string, decoded_at_va: VA where decoding routine is called,
# fva: function VA of decoding routine, characteristics: meta information dictionary for the
# identified memory location
DecodedString = namedtuple("DecodedString", ["va", "s", "decoded_at_va", "fva", "characteristics"])


class LocationType(Enum):
    STACK = 1
    GLOBAL = 2
    HEAP = 3


class ApiMonitor(viv_utils.emulator_drivers.Monitor):
    '''
    The ApiMonitor observes emulation and provides an interface
     for hooking API calls.
    '''
    def __init__(self, vw, function_index):
        viv_utils.emulator_drivers.Monitor.__init__(self, vw)
        self.function_index = function_index

    def apicall(self, emu, op, pc, api, argv):
        # overridden from Monitor
        self.d("apicall: %s %s %s %s %s", emu, op, pc, api, argv)

    def prehook(self, emu, op, startpc):
        # overridden from Monitor
        self.d("%s: %s", hex(startpc), op)

    def posthook(self, emu, op, endpc):
        # overridden from Monitor
        if op.mnem == "ret":
            try:
                self._check_return(emu, op)
            except Exception as e:
                self.d(str(e))

    def _check_return(self, emu, op):
        '''
        Ensure that the target of the return is within the allowed set of functions.
        Do nothing, if return address is valid. If return address is invalid:
        _fix_return modifies program counter and stack pointer if a valid return address is found
        on the stack or raises an Exception if no valid return address is found.
        '''
        function_start = self.function_index[op.va]
        return_addresses = self._get_return_vas(emu, function_start)

        if op.opers:
            # adjust stack in case of `ret imm16` instruction
            emu.setStackCounter(emu.getStackCounter() - op.opers[0].imm)

        return_address = self.getStackValue(emu, -4)
        if return_address not in return_addresses:
            self._logger.debug("Return address 0x%08X is invalid", return_address)
            self._fix_return(emu, return_address, return_addresses)
            # TODO return, handle Exception
        else:
            self._logger.debug("Return address 0x%08X is valid, returning", return_address)
            # TODO return?

    def _get_return_vas(self, emu, function_start):
        '''
        Get the list of valid addresses to which a function should return.
        '''
        return_vas = []
        callers = self._vw.getCallers(function_start)
        for caller in callers:
            call_op = emu.parseOpcode(caller)
            return_va = call_op.va + call_op.size
            return_vas.append(return_va)
        return return_vas

    def _fix_return(self, emu, return_address, return_addresses):
        '''
        Find a valid return address from return_addresses on the stack. Adjust the stack accordingly
        or raise an Exception if no valid address is found within the search boundaries.
        Modify program counter and stack pointer, so the emulator does not return to a garbage address.
        '''
        self.dumpStack(emu)
        NUM_ADDRESSES = 4
        pointer_size = emu.getPointerSize()
        STACK_SEARCH_WINDOW = pointer_size * NUM_ADDRESSES
        esp = emu.getStackCounter()
        for offset in xrange(0, STACK_SEARCH_WINDOW, pointer_size):
            ret_va_candidate = self.getStackValue(emu, offset)
            if ret_va_candidate in return_addresses:
                emu.setProgramCounter(ret_va_candidate)
                emu.setStackCounter(esp + offset + pointer_size)
                self._logger.debug("Returning to 0x%08X, adjusted stack:", ret_va_candidate)
                self.dumpStack(emu)
                return

        self.dumpStack(emu)
        raise Exception("No valid return address found...")

    def dumpStack(self, emu):
        '''
        Convenience debugging routine for showing
         state current state of the stack.
        '''
        esp = emu.getStackCounter()
        stack_str = ""
        for i in xrange(16, -16, -4):
            if i == 0:
                sp = "<= SP"
            else:
                sp = "%02d" % i
            stack_str = "%s\n0x%08X - 0x%08X %s" % (stack_str, (esp + i), self.getStackValue(emu, i), sp)
        self._logger.debug(stack_str)


def pointerSize(emu):
    '''
    Convenience method whose name might be more readable
     than fetching emu.imem_psize.
    Returns the size of a pointer in bytes for the given emulator.
    :rtype: int
    '''
    return emu.imem_psize


def popStack(emu):
    '''
    Remove the element at the top of the stack.
    :rtype: int
    '''
    v = emu.readMemoryFormat(emu.getStackCounter(), "<P")[0]
    emu.setStackCounter(emu.getStackCounter() + pointerSize(emu))
    return v


class GetProcessHeapHook(viv_utils.emulator_drivers.Hook):
    '''
    Hook and handle calls to GetProcessHeap, returning 0.
    '''
    def hook(self, callname, emu, callconv, api, argv):
        if callname == "kernel32.GetProcessHeap":
            # nop
            callconv.execCallReturn(emu, 0, len(argv))
            return True
        raise viv_utils.emulator_drivers.UnsupportedFunction()


def round(i, size):
    '''
    Round `i` to the nearest greater-or-equal-to multiple of `size`.

    :type i: int
    :type size: int
    :rtype: int
    '''
    if i % size == 0:
        return i
    return i + (i - (i % size))


class RtlAllocateHeapHook(viv_utils.emulator_drivers.Hook):
    '''
    Hook calls to RtlAllocateHeap, allocate memory in a "heap"
     section, and return pointers to this memory.
    The base heap address is 0x69690000.
    The max allocation size is 10 MB.
    '''
    def __init__(self, *args, **kwargs):
        super(RtlAllocateHeapHook, self).__init__(*args, **kwargs)
        self._heap_addr = 0x69690000

    MAX_ALLOCATION_SIZE = 10 * 1024 * 1024

    def _allocate_mem(self, emu, size):
        size = round(size, 0x1000)
        if size > self.MAX_ALLOCATION_SIZE:
            size = self.MAX_ALLOCATION_SIZE
        va = self._heap_addr
        self.d("RtlAllocateHeap: mapping %s bytes at %s", hex(size), hex(va))
        emu.addMemoryMap(va, envi.memory.MM_RWX, "[heap allocation]", "\x00" * (size + 4))
        emu.writeMemory(va, "\x00" * size)
        self._heap_addr += size
        return va

    def hook(self, callname, driver, callconv, api, argv):
        # works for kernel32.HeapAlloc
        if callname == "ntdll.RtlAllocateHeap":
            emu = driver
            size = driver.getStackValue(0xC)
            va = self._allocate_mem(emu, size)
            callconv.execCallReturn(emu, va, len(argv))
            return True
        raise viv_utils.emulator_drivers.UnsupportedFunction()


class AllocateHeap(RtlAllocateHeapHook):
    '''
    Hook calls to AllocateHeap and handle them like calls to RtlAllocateHeapHook.
    '''
    def __init__(self, *args, **kwargs):
        super(AllocateHeap, self).__init__(*args, **kwargs)

    def hook(self, callname, driver, callconv, api, argv):
        if callname == "kernel32.LocalAlloc" or \
           callname == "kernel32.GlobalAlloc" or \
           callname == "kernel32.VirtualAlloc":
            emu = driver
            # TODO dependant on calling convention, see issue #124
            size = driver.getStackValue(0x8)
            va = self._allocate_mem(emu, size)
            callconv.execCallReturn(emu, va, len(argv))
            return True
        raise viv_utils.emulator_drivers.UnsupportedFunction()


class MallocHeap(RtlAllocateHeapHook):
    '''
    Hook calls to malloc and handle them like calls to RtlAllocateHeapHook.
    '''
    def __init__(self, *args, **kwargs):
        super(MallocHeap, self).__init__(*args, **kwargs)

    def hook(self, callname, driver, callconv, api, argv):
        if callname == "msvcrt.malloc":
            emu = driver
            size = driver.getStackValue(0x4)
            va = self._allocate_mem(emu, 0x100)  # TODO hard-coded!
            callconv.execCallReturn(emu, va, len(argv))
            return True
        raise viv_utils.emulator_drivers.UnsupportedFunction()


class ExitProcessHook(viv_utils.emulator_drivers.Hook):
    '''
    Hook calls to ExitProcess and stop emulation when these are hit.
    '''
    def __init__(self, *args, **kwargs):
        super(ExitProcessHook, self).__init__(*args, **kwargs)

    def hook(self, callname, driver, callconv, api, argv):
        if callname == "kernel32.ExitProcess":
            raise viv_utils.emulator_drivers.StopEmulation()


def is_import(emu, va):
    '''
    Return True if the given VA is that of an imported function.
    '''
    # TODO: also check location type
    t = emu.getVivTaint(va)
    if t is None:
        return False
    return t[1] == "import"


# A snapshot represents the current state of the CPU and memory
Snapshot = namedtuple("Snapshot",
        [   "memory",  # The memory snapshot, type: envi.MemorySnapshot
            "sp",  # The current stack counter, type: int
            "pc",  # The current instruction pointer, type: int
            ])


def make_snapshot(emu):
    '''
    Create a snapshot of the current CPU and memory.

    :rtype: Snapshot
    '''
    return Snapshot(emu.getMemorySnap(), emu.getStackCounter(), emu.getProgramCounter())


# A Delta represents the pair of snapshots from before and
#  after an operation. It facilitates diffing the state of
#  an emalutor.
Delta = namedtuple("Delta",
        [   "pre_snap",  # type: Snapshot
            "post_snap",  # type: Snapshot
            ])


class DeltaCollectorHook(viv_utils.emulator_drivers.Hook):
    """
    hook that collects Deltas at each imported API call.
    """
    def __init__(self, pre_snap):
        super(DeltaCollectorHook, self).__init__()

        self._pre_snap = pre_snap
        # this is a public field
        self.deltas = []

    def hook(self, callname, driver, callconv, api, argv):
        if is_import(driver._emu, driver._emu.getProgramCounter()):
            self.deltas.append(Delta(self._pre_snap, make_snapshot(driver._emu)))


def emulate_function(emu, function_index, fva, return_address, max_instruction_count):
    '''
    Emulate a function and collect snapshots at each interesting place.
    These interesting places include calls to imported API functions
     and the final state of the emulator.
    Emulation continues until the return address is hit, or
     the given max_instruction_count is hit.
    Some library functions are shimmed, such as memory allocation routines.
    This helps "normal" routines emulate correct using standard library function.
    These include:
      - GetProcessHeap
      - RtlAllocateHeap
      - AllocateHeap
      - malloc

    :type emu: envi.Emulator
    :type function_index: viv_utils.FunctionIndex
    :type fva: int
    :param fva: The start address of the function to emulate.
    :int return_address: int
    :param return_address: The expected return address of the function.
     Emulation stops here.
    :type max_instruction_count: int
    :param max_instruction_count: The max number of instructions to emulate.
     This helps avoid unexpected infinite loops.
    :rtype: Sequence[Delta]
    '''
    pre_snap = make_snapshot(emu)
    delta_collector = DeltaCollectorHook(pre_snap)

    try:
        floss_logger.debug("Emulating function at 0x%08X", fva)
        driver = viv_utils.emulator_drivers.DebuggerEmulatorDriver(emu)
        monitor = ApiMonitor(emu.vw, function_index)
        driver.add_monitor(monitor)
        driver.add_hook(delta_collector)
        driver.add_hook(GetProcessHeapHook())
        driver.add_hook(RtlAllocateHeapHook())
        driver.add_hook(AllocateHeap())
        driver.add_hook(MallocHeap())
        driver.add_hook(ExitProcessHook())
        driver.runToVa(return_address, max_instruction_count)
    except viv_utils.emulator_drivers.InstructionRangeExceededError:
        floss_logger.debug("Halting as emulation has escaped!")
    except envi.InvalidInstruction:
        floss_logger.debug("vivisect encountered an invalid instruction. will continue processing.",
                exc_info=True)
    except envi.UnsupportedInstruction:
        floss_logger.debug("vivisect encountered an unsupported instruction. will continue processing.",
                exc_info=True)
    except envi.BreakpointHit:
        floss_logger.debug("vivisect encountered an unexpected emulation breakpoint. will continue processing.",
                exc_info=True)
    except viv_utils.emulator_drivers.StopEmulation as e:
        pass
    except Exception:
        floss_logger.debug("vivisect encountered an unexpected exception. will continue processing.",
                exc_info=True)
    floss_logger.debug("Ended emulation at 0x%08X", emu.getProgramCounter())

    deltas = delta_collector.deltas
    deltas.append(Delta(pre_snap, make_snapshot(emu)))
    return deltas
