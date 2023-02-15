import sys

from pyvism.frontend.vmbc.tools import AnyInstruction
from pyvism.frontend.vmbc.tools import VMState


class VM:
	def __init__(self, *, strict_mode: bool = True) -> None:
		self.state = VMState()
		self.strict_mode = strict_mode

	def run(self, bytecode: list[AnyInstruction]) -> None:
		for instr in bytecode:
			try:
				self.state = instr.run(self.state)
			except Exception as e:
				print(
					f"\x1b[1;31mRuntime exception:",
					f"  {type(e).__name__}: {e}",
					f"\n[Illegal operation]\x1b[22;39m",
					sep="\n",
					file=sys.stderr,
				)
				if self.strict_mode:
					return None