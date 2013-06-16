class Cell(object):
	def __init__(self, target, key, writable=False, ctor=None):
		super(Cell, self).__init__()
		self._target = target
		self._key = key
		self._writable = writable
		self._ctor = ctor
		self._getter = getattr(self._target, 'get_' + key)
		if writable:
			self._setter = getattr(self._target, 'set_' + key)
		else:
			self._setter = None
	
	def isBlock(self):
		return False
	
	def key(self):
		return self._key
	
	def ctor(self):
		return self._ctor
	
	def get(self):
		return self._getter()
	
	def set(self, value):
		if not self.isWritable():
			raise Exception, 'Not writable.'
		return self._setter(value)
	
	def isWritable(self):
		return self._writable
	
	def persists(self):
		return self._writable

	def description(self):
		if str(self._ctor) == 'sdr.top.SpectrumTypeStub':
			# TODO: eliminate special case
			typename = 'spectrum'
		else:
			typename = None
		return {
			'kind': 'value',
			'type': typename,
			'writable': self.isWritable(),
			'current': self.get()
		}


class BlockCell(object):
	def __init__(self, target, key):
		super(BlockCell, self).__init__()
		self._target = target
		self._key = key
	
	def isBlock(self):
		return True
	
	def key(self):
		return self._key
	
	def ctor(self):
		return None
	
	def getBlock(self):
		# TODO method-based access
		return getattr(self._target, self._key)
	
	def getMembers(self):
		return self.getBlock().state()
	
	def get(self):
		return self.getBlock().state_to_json()
	
	def set(self, value):
		self.getBlock().state_from_json(value)
	
	def isWritable(self):
		return False
	
	def persists(self):
		return True
	
	def description(self):
		return self.getBlock().state_description()

class ExportedState(object):
	def state_def(self, callback):
		pass
	def state(self):
		if not hasattr(self, '_ExportedState__cache'):
			cache = {}
			def callback(cell):
				cache[cell.key()] = cell
			self.state_def(callback)
			self.__cache = cache
		return self.__cache
	def state_to_json(self):
		state = {}
		for key, cell in self.state().iteritems():
			if cell.persists():
				state[key] = cell.get()
		return state
	def state_from_json(self, state):
		cells = self.state()
		defer = []
		for key in state:
			# TODO: gracefully handle nonexistent or not-writable
			cell = cells[key]
			if cell.isBlock():
				defer.append(key)
			else:
				cells[key].set(state[key])
		# blocks are deferred because the specific blocks may depend on other keys
		for key in defer:
			cells[key].set(state[key])
	def state_description(self):
		childDescs = {}
		description = {
			'kind': 'block',
			'children': childDescs
		}
		for key, cell in self.state().iteritems():
			# TODO: include URLs explicitly in desc format
			childDescs[key] = cell.description()
		return description
		