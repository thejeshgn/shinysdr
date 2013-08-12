#!/usr/bin/env python

import gnuradio
import gnuradio.fft.logpwrfft
from gnuradio import audio
from gnuradio import blocks
from gnuradio import gr
from gnuradio.eng_option import eng_option
from gnuradio.filter import firdes
from optparse import OptionParser
from sdr.values import ExportedState, Cell, CollectionState, BlockCell, MsgQueueCell, Enum, Range, NoneES
import sdr.receiver
import sdr.receivers.vor

from twisted.internet import reactor

import time


class SpectrumTypeStub:
	pass


class ReceiverCollection(CollectionState):
	def __init__(self, table, top):
		CollectionState.__init__(self, table, dynamic=True)
		self.__top = top
	
	def state_insert(self, key, desc):
		(key, receiver) = self.__top.add_receiver(desc['mode'], key=key)
		receiver.state_from_json(desc)
	
	def create_child(self, desc):
		(key, receiver) = self.__top.add_receiver(desc['mode'])
		receiver.state_from_json(desc)
		return key
		
	def delete_child(self, key):
		self.__top.delete_receiver(key)


class Top(gr.top_block, ExportedState):

	def __init__(self, sources={}):
		gr.top_block.__init__(self, "SDR top block")
		self._running = False

		# Configuration
		self._sources = dict(sources)
		self.source_name = 'audio'  # placeholder - TODO be nothing instead
		self.audio_rate = audio_rate = 44100
		self.spectrum_resolution = 4096
		self.spectrum_rate = 30

		# Blocks etc.
		self.source = None
		self.spectrum_queue = None
		self.spectrum_sink = None
		self.spectrum_fft_block = None
		
		# Receiver blocks (multiple, eventually)
		self._receivers = {}
		self._receiver_valid = {}

		# kludge for using collection like block - TODO: better architecture
		self.sources = CollectionState(self._sources)
		self.receivers = ReceiverCollection(self._receivers, self)
		
		# Audio stream bits
		num_audio_channels = 2
		self.audio_stream_join = blocks.streams_to_vector(gr.sizeof_float, num_audio_channels)
		self.audio_stream_queue = gr.msg_queue(limit=100)
		self.audio_stream_sink = blocks.message_sink(
			gr.sizeof_float * num_audio_channels,
			self.audio_stream_queue,
			True)
		
		# Flags, other state
		self.__needs_audio_restart = True
		self.__needs_spectrum = True
		self.__needs_reconnect = True
		self.input_rate = None
		self.input_freq = None
		self.receiver_key_counter = 0
		self.receiver_default_state = {}
		self.last_wall_time = time.time()
		self.last_cpu_time = time.clock()
		self.last_cpu_use = 0
		
		self._do_connect()

	def add_receiver(self, mode, key=None):
		if len(self._receivers) >= 100:
			# Prevent storage-usage DoS attack
			raise Error('Refusing to create more than 100 receivers')
		
		if key is not None:
			assert key not in self._receivers
		else:
			while True:
				key = base26(self.receiver_key_counter)
				self.receiver_key_counter += 1
				if key not in self._receivers:
					break
		
		if len(self._receivers) > 0:
			arbitrary = self._receivers.itervalues().next()
			defaults = arbitrary.state_to_json()
		else:
			defaults = self.receiver_default_state
		
		receiver = self._make_receiver(mode, defaults, key)
		
		self._receivers[key] = receiver
		self._receiver_valid[key] = False
		
		self.__needs_reconnect = True
		self._do_connect()
		
		return (key, receiver)

	def delete_receiver(self, key):
		assert key in self._receivers
		receiver = self._receivers[key]
		
		# save defaults for use if about to become empty
		if len(self._receivers) == 1:
			self.receiver_default_state = receiver.state_to_json()
		
		del self._receivers[key]
		del self._receiver_valid[key]
		self.__needs_reconnect = True
		self._do_connect()

	def _do_connect(self):
		"""Do all reconfiguration operations in the proper order."""
		if self.__needs_audio_restart:
			print 'Rebuilding audio blocks'
			self.__needs_reconnect = True

		rate_changed = False
		if self.source is not self._sources[self.source_name] or self.__needs_audio_restart:
			print 'Switching source'
			self.__needs_reconnect = True
			
			def tune_hook():
				reactor.callLater(self.source.get_tune_delay(), tune_hook_actual)
			def tune_hook_actual():
				if self.source is not this_source:
					return
				freq = this_source.get_freq()
				self.input_freq = freq
				for key, receiver in self._receivers.iteritems():
					receiver.set_input_center_freq(freq)
					self._update_receiver_validity(key)

			this_source = self._sources[self.source_name]
			this_source.set_tune_hook(tune_hook)
			self.source = this_source
			this_rate = this_source.get_sample_rate()
			rate_changed = self.input_rate != this_rate
			self.input_rate = this_rate
			self.input_freq = this_source.get_freq()
		
		# clear separately because used twice above
		self.__needs_audio_restart = False
		
		if self.__needs_spectrum or rate_changed:
			print 'Rebuilding spectrum FFT'
			self.__needs_spectrum = False
			self.__needs_reconnect = True
			
			self.spectrum_queue = gr.msg_queue(limit=10)
			self.spectrum_sink = blocks.message_sink(
				self.spectrum_resolution * gr.sizeof_float,
				self.spectrum_queue,
				True) # dont_block
			self.spectrum_fft_block = gnuradio.fft.logpwrfft.logpwrfft_c(
				sample_rate=self.input_rate,
				fft_size=self.spectrum_resolution,
				ref_scale=2,
				frame_rate=self.spectrum_rate,
				avg_alpha=1.0,
				average=False,
			)

		if rate_changed:
			print 'Rebuilding receivers'
			for key in self._receivers:
				self._rebuild_receiver_nodirty(key)
			self.__needs_reconnect = True

		if self.__needs_reconnect and self.source.needs_renew():
			print 'Renewing source'
			self.source = self.source.renew()
			self._sources[self.source_name] = self.source

		if self.__needs_reconnect:
			print 'Reconnecting'
			self.__needs_reconnect = False
			
			self.lock()
			self.disconnect_all()


			# recreated each time because reusing an add_ff w/ different
			# input counts fails; TODO: report/fix bug
			audio_sum_l = blocks.add_ff()
			audio_sum_r = blocks.add_ff()

			self.connect(self.source, self.spectrum_fft_block, self.spectrum_sink)

			audio_sum_index = 0
			for key, receiver in self._receivers.iteritems():
				self._receiver_valid[key] = receiver.get_is_valid()
				if self._receiver_valid[key]:
					if audio_sum_index >= 6:
						# Sanity-check to avoid burning arbitrary resources
						# TODO: less arbitrary constant; communicate this restriction to client
						print 'Refusing to connect more than 6 receivers'
						break
					self.connect(self.source, receiver)
					self.connect((receiver, 0), (audio_sum_l, audio_sum_index))
					self.connect((receiver, 1), (audio_sum_r, audio_sum_index))
					audio_sum_index += 1
		
			if audio_sum_index > 0:
				# connect audio output only if there is at least one input
				# sink is recreated each time to workaround problem with restarting audio sinks on Mac OS X. TODO: do only on OS X, or report/fix gnuradio bug
				audio_sink = audio.sink(self.audio_rate, "", False)
				self.connect(audio_sum_l, (self.audio_stream_join, 0))
				self.connect(audio_sum_r, (self.audio_stream_join, 1))
				self.connect(self.audio_stream_join, self.audio_stream_sink)
				self.connect(audio_sum_l, (audio_sink, 0))
				self.connect(audio_sum_r, (audio_sink, 1))
		
			self.unlock()

	def _update_receiver_validity(self, key):
		receiver = self._receivers[key]
		if receiver.get_is_valid() != self._receiver_valid[key]:
			self.__needs_reconnect = True
			self._do_connect()

	def state_def(self, callback):
		super(Top, self).state_def(callback)
		callback(Cell(self, 'running', writable=True, ctor=bool))
		callback(Cell(self, 'source_name', writable=True,
			ctor=Enum(dict([(k, str(v)) for (k, v) in self._sources.iteritems()]))))
		callback(Cell(self, 'input_rate', ctor=int))
		callback(Cell(self, 'audio_rate', ctor=int))
		callback(Cell(self, 'spectrum_resolution', writable=True, ctor=
			Range(2, 4096, logarithmic=True, integer=True)))
		callback(Cell(self, 'spectrum_rate', writable=True, ctor=
			Range(1, 60, logarithmic=True, integer=False)))
		callback(MsgQueueCell(self, 'spectrum_fft', fill=True, ctor=SpectrumTypeStub))
		callback(BlockCell(self, 'sources'))
		callback(BlockCell(self, 'source', persists=False))
		callback(BlockCell(self, 'receivers'))
		callback(Cell(self, 'cpu_use', ctor=float))

	def start(self):
		self.__needs_audio_restart = True
		self._do_connect()  # audio sink workaround
		super(Top, self).start()

	def get_running(self):
		return self._running
	
	def set_running(self, value):
		if value != self._running:
			self._running = value
			if value:
				self.start()
			else:
				self.stop()
				self.wait()

	def get_source_name(self):
		return self.source_name
	
	def set_source_name(self, value):
		if value == self.source_name:
			return
		if value not in self._sources:
			raise ValueError('Source %r does not exist' % (value,))
		self.source_name = value
		self._do_connect()

	def _rebuild_receiver(self, key, mode=None):
		self._rebuild_receiver_nodirty(key, mode)
		self.__needs_reconnect = True
		self._do_connect()

	def _rebuild_receiver_nodirty(self, key, mode=None):
		receiver = self._receivers[key]
		if mode is None:
			mode = receiver.get_mode()
		self._receivers[key] = self._make_receiver(mode, receiver.state_to_json(), key)

	def _make_receiver(self, kind, state, key):
		'''Returns the receiver.'''

		if kind == 'IQ':
			clas = sdr.receiver.IQReceiver
		elif kind == 'NFM':
			clas = sdr.receiver.NFMReceiver
		elif kind == 'WFM':
			clas = sdr.receiver.WFMReceiver
		elif kind == 'AM':
			clas = sdr.receiver.AMReceiver
		elif kind == 'USB' or kind == 'LSB':
			clas = sdr.receiver.SSBReceiver
		elif kind == 'VOR':
			clas = sdr.receivers.vor.VOR
		else:
			raise ValueError('Unknown mode: ' + kind)

		# TODO: extend state_from_json so we can decide to load things with keyword args and lose the init dict/state dict distinction
		init = {}
		state = state.copy()  # don't modify arg
		if 'mode' in state: del state['mode']

		# TODO remove this special case for WFM receiver
		if kind == 'WFM':
			if 'stereo' in state:
				init['stereo'] = state['stereo']
			if 'audio_filter' in state:
				init['audio_filter'] = state['audio_filter']

		facet = TopFacetForReceiver(self, key)
		receiver = clas(
			mode=kind,
			input_rate=self.input_rate,
			input_center_freq=self.input_freq,
			audio_rate=self.audio_rate,
			control_hook=facet,
			**init
		)
		receiver.state_from_json(state)
		# until _enabled, ignore any callbacks resulting from the state_from_json initialization
		facet._enabled = True
		return receiver

	def get_input_rate(self):
		return self.input_rate

	def get_audio_rate(self):
		return self.audio_rate
	
	def get_spectrum_resolution(self):
		return self.spectrum_resolution

	def set_spectrum_resolution(self, spectrum_resolution):
		self.spectrum_resolution = spectrum_resolution
		self.__needs_spectrum = True
		self._do_connect()

	def get_spectrum_rate(self):
		return self.spectrum_rate

	def set_spectrum_rate(self, value):
		self.spectrum_rate = value
		self.spectrum_fft_block.set_vec_rate(value)
	
	def get_spectrum_fft_info(self):
		return self.input_freq
	
	def get_spectrum_fft_queue(self):
		return self.spectrum_queue
	
	def get_cpu_use(self):
		cur_wall_time = time.time()
		elapsed_wall = cur_wall_time - self.last_wall_time
		if elapsed_wall > 0.5:
			cur_cpu_time = time.clock()
			elapsed_cpu = cur_cpu_time - self.last_cpu_time
			self.last_wall_time = cur_wall_time
			self.last_cpu_time = cur_cpu_time
			self.last_cpu_use = round(elapsed_cpu / elapsed_wall, 2)
		return self.last_cpu_use


class TopFacetForReceiver(object):
	def __init__(self, top, key):
		self._top = top
		self._key = key
		self._enabled = False # assigned outside
	
	def revalidate(self):
		if self._enabled:
			self._top._update_receiver_validity(self._key)
	
	def rebuild_me(self):
		assert self._enabled
		self._top._rebuild_receiver(self._key)
	
	def replace_me(self, mode):
		assert self._enabled
		self._top._rebuild_receiver(self._key, mode=mode)


def base26(x):
	'''not quite base 26, actually, because it has no true zero digit'''
	if x < 26:
		return 'abcdefghijklmnopqrstuvwxyz'[x]
	else:
		return base26(x // 26 - 1) + base26(x % 26)
