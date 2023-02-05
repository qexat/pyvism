from io import StringIO
from typing import Any, Callable, TextIO

from result import Result, Ok, Err

from pyvism.constants import (
	MEMORY_MAX_ADDR,
	NULL,
	REGISTER_MAX_ADDR,
	confusable_symbols,
	get_name,
)
from pyvism.runtime.builtins import (
	DEBUG_MODE_CHAR,
	DISCARDED_CHARS,
	ESCAPABLE_CHARS,
	NS_MODES,
	PRGM_MODE_CHAR,
	STREAM_IDS,
	AssignKind,
	ConstTypeDef,
	MacroKind,
	Mode,
	ModeBufferMap,
	Target,
	TargetKind,
	TypeDef,
	UnsetType,
	VarTypeDef,
	instruction,
	mnemonic,
)
from pyvism.runtime.errors import (
	Error,
	ErrorLine,
	InfoLine,
	VismSyntaxError,
	VismTypeError,
	VismValueError,
)
from pyvism import instructions
from pyvism.runtime.utils import (
	evaluate,
	is_known_stream,
	is_valid_address,
)


__all__ = ("Compiler",)


class Compiler:
	def __init__(self, file: TextIO, repl_mode: bool = False) -> None:
		if MEMORY_MAX_ADDR < REGISTER_MAX_ADDR:
			raise RuntimeError("illegal register max address")

		self.file: TextIO = file

		# StringIO is marked as having a `name` property
		# but in reality it does not at runtime
		if isinstance(self.file, StringIO):
			self.file.name = "<stdin>"

		self.lines: list[str] = self.file.read().splitlines()
		self.line_index: int = 0
		self.pos = 0

		self.memory_typedefs: list[TypeDef] = [ConstTypeDef(UnsetType)] * MEMORY_MAX_ADDR
		self.registers: list[int] = list(range(REGISTER_MAX_ADDR))

		self.mode: Mode = Mode.Normal
		self.assign_type: AssignKind | None = None
		# Default target is the null stream
		self.target_kind: TargetKind = TargetKind.Stream
		self.assign_addr: int = NULL

		self.mode_buffers = ModeBufferMap.new()
		self.mode_spos: int = 0

		self.char_escaping: bool = False

		self.__bytecode: list[instruction[*tuple[Any, ...]]] = []
		self.__operations: list[instruction[*tuple[Any, ...]]] = []

		self.errors: list[Error] = []
		self.repl_mode = repl_mode

	@property
	def end(self) -> int:
		return len(self.line)

	@property
	def line(self) -> str:
		return self.lines[self.line_index]

	@property
	def line_number(self) -> int:
		return self.line_index + 1

	@property
	def cur_char(self) -> str:
		return self.line[self.pos]

	@property
	def bytecode(self):
		return self.__bytecode

	def is_eol(self) -> bool:
		return self.pos == self.end

	def mode_request(self, special_char: str) -> bool:
		return self.line[self.pos] == special_char and not self.char_escaping

	def program_mode_change_request(self) -> bool:
		return self.mode_request(PRGM_MODE_CHAR)

	def macro_mode_request(self) -> bool:
		return self.mode_request(DEBUG_MODE_CHAR)

	def get_targets_typedefs(self, targets: tuple[Target, ...]) -> tuple[TypeDef, ...]:
		return tuple(self.memory_typedefs[target.id] for target in targets)

	def operation_typecheck(
		self, mnemonic: mnemonic[*tuple[Any, ...]], targets_types: tuple[type, ...]
	) -> Result[None, None]:
		if mnemonic in instructions.op_type_specialization:
			combinations = instructions.op_type_specialization[mnemonic]

			if not targets_types in combinations:
				return Err(None)

		return Ok(None)

	def buffer_operation(self, mnemonic: mnemonic[*tuple[Any, ...]]) -> None:
		targets = tuple(
			Target(TargetKind.Memory, reg) for reg in self.registers[: mnemonic.args]
		)

		targets_typedefs = self.get_targets_typedefs(targets)
		targets_types = tuple(td.type for td in targets_typedefs)
		targets_types_names = tuple(map(get_name, targets_types))

		if self.operation_typecheck(mnemonic, targets_types).is_err():
			targets_types_listing = (
				", ".join(targets_types_names[:-1]) + f" and {targets_types_names[-1]}"
			)
			self.errors.append(
				VismTypeError(
					f"cannot {mnemonic.func.__name__} {targets_types_listing}",
					self.file.name,
					ErrorLine(
						self.line_number,
						self.line,
						self.pos,
						self.pos + 1,
						f"no implementation for {targets_types_listing}",
					),
					[
						InfoLine(
							td.line_number,
							self.lines[td.line_number - 1],
							td.spos,
							td.epos,
							f"arg {i} was first defined here with type {td.type.__name__}",
						)
						for i, td in enumerate(targets_typedefs, 1)
						if isinstance(td, VarTypeDef)
					],
				)
			)

		if not self.errors:
			mnemonic_type_map = instructions.op_type_specialization.get(mnemonic, None)

			final_mnemonic = (
				mnemonic
				if mnemonic_type_map is None
				else mnemonic_type_map[targets_types] or mnemonic
			)

			self.__operations.append((final_mnemonic)(*targets))

	def process_char(self, char: str) -> None:
		if TargetKind.contains(char):
			self.target_kind = TargetKind(char)
			self.mode = Mode.Select
		elif char in instructions.char_map:
			self.buffer_operation(instructions.char_map[char])
		elif char == "!":
			self.__bytecode.extend(self.__operations)
			self.__operations.clear()
		else:
			hint = (
				f"did you mean `{confusable_symbols[char]}`?"
				if char in confusable_symbols
				else None
			)

			self.errors.append(
				VismSyntaxError(
					f"unknown symbol {char!r}",
					self.file.name,
					ErrorLine(
						self.line_number,
						self.line,
						self.pos,
						self.pos + 1,
						"unknown symbol",
					),
					[],
					hint,
				)
			)

	def escape_char(self, char: str) -> None:
		if not char in ESCAPABLE_CHARS:
			self.errors.append(
				VismValueError(
					f"invalid escape character '\\{char}'",
					self.file.name,
					ErrorLine(
						self.line_number,
						self.line,
						self.pos - 1,
						self.pos + 1,
						"invalid escape character",
					),
					[],
				)
			)
			return None
		self.mode_buffers[self.mode].write(ESCAPABLE_CHARS[char])
		self.char_escaping = False

	def buffer_char(self, char: str) -> None:
		if self.char_escaping:
			self.escape_char(char)
		else:
			if char == "\\" and self.mode is Mode.Assign:
				self.char_escaping = True
			else:
				self.mode_buffers[self.mode].write(char)

	def get_target_name(self) -> str:
		if self.target_kind is TargetKind.Stream and is_known_stream(self.assign_addr):
			return list(STREAM_IDS.keys())[self.assign_addr + 1]
		return f"{self.target_kind.name}[{self.assign_addr:#04x}]"

	def get_target_typedef(self) -> TypeDef:
		match self.target_kind:
			case TargetKind.Memory:
				return self.memory_typedefs[self.assign_addr]
			case TargetKind.Register:
				return ConstTypeDef(int)
			case TargetKind.Stream:
				return ConstTypeDef(UnsetType)

	def assign_typecheck(self, value: Any) -> Result[None, None]:
		target_typedef = self.get_target_typedef()
		if (target_type := target_typedef.type) is UnsetType:
			return Ok(None)

		infos = (
			[
				InfoLine(
					target_typedef.line_number,
					self.lines[target_typedef.line_number - 1],
					target_typedef.spos,
					target_typedef.epos,
					f"was defined here as {target_type.__name__}",
				)
			]
			if isinstance(target_typedef, VarTypeDef)
			else []
		)

		if not isinstance(value, target_type):
			self.errors.append(
				VismTypeError(
					"mismatched types",
					self.file.name,
					ErrorLine(
						self.line_number,
						self.line,
						self.mode_spos,
						self.pos,
						"unmatching type",
					),
					infos,
					f"expected type {target_type.__name__}, found {type(value).__name__}",
				)
			)

			return Err(None)
		return Ok(None)

	def process_buffered(self) -> None:
		mode_buffer = self.mode_buffers[self.mode]
		str_value = mode_buffer.getvalue()

		match self.mode:
			case Mode.Select:
				if not is_valid_address(str_value):
					self.errors.append(
						VismValueError(
							f"invalid address {str_value!r}",
							self.file.name,
							ErrorLine(
								self.line_number,
								self.line,
								self.mode_spos,
								self.pos,
								"invalid address",
							),
							[],
						)
					)
					return None
				self.assign_addr = int(str_value, base=16)
			case Mode.Assign:
				if self.assign_type is None:
					raise RuntimeError  # supposedly unreachable

				value = evaluate(str_value, self.assign_type)

				match value:
					case Ok():
						value = value.unwrap()
					case Err():
						self.errors.append(
							VismValueError(
								f"invalid literal {str_value!r}",
								self.file.name,
								ErrorLine(
									self.line_number,
									self.line,
									self.mode_spos,
									self.pos,
									"invalid literal",
								),
								[],
							)
						)
						return None

				if self.assign_typecheck(value).is_err():
					return None

				match self.target_kind:
					case TargetKind.Memory:
						self.memory_typedefs[self.assign_addr] = VarTypeDef(
							type(value), self.line_number, self.mode_spos, self.pos
						)
					case TargetKind.Register:
						self.registers[self.assign_addr] = value
					case TargetKind.Stream:
						value = str(value)

				target = Target(self.target_kind, self.assign_addr)

				match self.target_kind:
					case TargetKind.Memory:
						self.__bytecode.append(instructions.mov(target, value))
					case TargetKind.Stream:
						self.__bytecode.append(instructions.write(target.id, value))
					case TargetKind.Register:
						pass  # registers only exist at compile time
			case _:
				pass

		self.mode_buffers.reset_buffer(self.mode)

	def change_mode(self) -> None:
		if self.is_eol():
			self.errors.append(
				VismSyntaxError(
					"unexpected end of line",
					self.file.name,
					ErrorLine(
						self.line_number,
						self.line,
						self.pos,
						self.pos + 1,
						"expected mode character here",
					),
					[],
					"try adding one of the following candidates:",
					[f"`{c}`" for c in NS_MODES.keys()],
				)
			)
			return None

		char = self.cur_char

		mode = NS_MODES.get(char, None)

		if mode is None:
			self.errors.append(
				VismValueError(
					f"invalid mode {char!r}",
					self.file.name,
					ErrorLine(
						self.line_number,
						self.line,
						self.pos - 1,
						self.pos + 1,
						"invalid mode",
					),
					[],
					"try using one of the following candidates:",
					[f"`^{c}`" for c in NS_MODES.keys()],
				)
			)
			return None

		self.mode = mode
		self.mode_spos = self.pos + 1
		self.assign_type = AssignKind(char) if AssignKind.contains(char) else None

	def run_macro(self) -> None:
		if self.is_eol():
			self.errors.append(
				VismSyntaxError(
					"unexpected end of line",
					self.file.name,
					ErrorLine(
						self.line_number,
						self.line,
						self.pos,
						self.pos + 1,
						"expected mode character here",
					),
					[],
					"try adding one of the following candidates:",
					[f"`{c}`" for c in MacroKind.values()],
				)
			)
			return None

		char = self.cur_char

		if not MacroKind.contains(char):
			self.errors.append(
				VismValueError(
					f"macro '?{char}' does not exist",
					self.file.name,
					ErrorLine(
						self.line_number,
						self.line,
						self.pos,
						self.pos + 1,
						"this macro does not exist",
					),
					[],
					"Try one of the following candidates:",
					[f"`?{value}`" for value in MacroKind.values()],
				)
			)
			return None

		MacroMap[MacroKind(char)](self)

	def is_discarded_char(self) -> bool:
		return self.mode is not Mode.Assign and self.cur_char in DISCARDED_CHARS

	def compile(self) -> Result[list[instruction[*tuple[Any, ...]]], list[Error]]:
		self.__bytecode.clear()

		while self.line_index < len(self.lines):
			while self.pos < self.end:
				if self.program_mode_change_request():
					self.process_buffered()
					self.pos += 1
					self.change_mode()
				elif self.macro_mode_request():
					self.process_buffered()
					self.pos += 1
					self.run_macro()
				elif not self.is_discarded_char():
					if self.mode is Mode.Normal:
						self.process_char(self.cur_char)
					else:
						self.buffer_char(self.cur_char)

				if self.errors:
					break

				self.pos += 1

			if self.errors:
				break

			self.process_buffered()
			self.line_index += 1
			self.pos = 0

		return Err(self.errors) if self.errors else Ok(self.__bytecode)


class Macro:
	@staticmethod
	def debug(compiler: Compiler) -> None:
		print("\x1b[2m" + " DEBUG ".center(80, "=") + "\x1b[22m")
		for instruction in compiler.bytecode:
			print(instruction)
		print("\x1b[2m" + "=" * 80 + "\x1b[22m")


MacroMap: dict[MacroKind, Callable[[Compiler], None]] = {
	MacroKind.Debug: Macro.debug,
}
