import struct, traceback
from io import SEEK_END

from playground.common.datastructures import Bijection
from playground.common.io import HighPerformanceStreamIO
from playground.common import Version as PacketDefinitionVersion
from playground.common import ReturnOrientedGenerator

from playground.network.packet.fieldtypes.attributes import Optional, ExplicitTag, MaxValue, Bits
from playground.network.packet.fieldtypes import ComplexFieldType, PacketFieldType, UINT, INT, BOOL, \
                                                    PacketFields, NamedPacketType, ListFieldType, \
                                                    StringFieldType, BufferFieldType, FloatFieldType

from .PacketEncoderBase import PacketEncoderBase
from .PacketEncodingError import PacketEncodingError
from .AbstractStreamAdapter import AbstractStreamAdapter

import logging
logger = logging.getLogger(__name__)

DECODE_WAITING_FOR_STREAM = PacketEncoderBase.DECODE_WAITING_FOR_STREAM

UNICODE_ENCODING = "utf-8" # used for converting strings to bytes and back.

class EncoderStreamAdapter(AbstractStreamAdapter):

    def __init__(self, stream, max_size=None):
        super().__init__(stream)
        self._max_size = max_size
        
    def set_max_size(self, max_size):
        self._max_size = max_size
        
    def pack(self, packCode, *args):
        return self._stream.write(struct.pack(packCode, *args))
        
    def unpack(self, packCode):
        g = ReturnOrientedGenerator(self.unpackIterator(packCode))
        for waitingForStream in g: pass
        return g.result()
        
    def unpackIterator(self, packCode):
        unpackSize = struct.calcsize(packCode)
        if self._max_size and unpackSize > self._max_size:
            raise Exception("Invalid packet. Unpack size of {} exceeds limit of {}".format(unpackSize, self._max_size))
        while self.available() < unpackSize:
            yield DECODE_WAITING_FOR_STREAM
        unpackChunk = self.read(unpackSize)
        try:
            unpackedData = struct.unpack(packCode, unpackChunk)
        except Exception as unpackError:
            raise PacketEncodingError("Unpack of {} failed.".format(packCode)) from unpackError
        if len(unpackedData) == 1:
            return unpackedData[0]
        else:
            return unpackedData

def iterateClassAncestors(cls, terminals=None):
    if terminals == None:
        terminals = []
        
    queue = [cls]
    while queue:
        nextClass = queue.pop(0)
        if nextClass not in terminals:
            for base in nextClass.__bases__:
                queue.append(base)
        yield nextClass

class PlaygroundStandardPacketEncoder(PacketEncoderBase):
    __TypeEncoders = {}
    
    @classmethod
    def _GetTypeKey(self, encodingType):
        """
        Two scenarios:
        
        1. A Complex Type. We have to get the specific data type and generalizations
        2. An instance of PacketFieldType or class. Get the class and generalizations
        """
        
        specificEncodingType = None
        complexType = None
        
        # Unbox instances to classes where necessary
        if isinstance(encodingType, ComplexFieldType):
            complexType  = encodingType
            specificEncodingType = complexType.dataType()
            encodingType = encodingType.__class__
            
            if isinstance(specificEncodingType, PacketFieldType):
                specificEncodingType = specificEncodingType.__class__
            
        elif isinstance(encodingType, PacketFieldType):
            encodingType = encodingType.__class__

        try:
            if not issubclass(encodingType, PacketFieldType):
                raise Exception("Playground Standard Packet Encoder only registers proper PacketFieldType's.")
        except:
            raise Exception("Playground Standard Packet Encoder only registers proper PacketFieldType's")
        
        for encodingTypeClass in iterateClassAncestors(encodingType, terminals=[PacketFieldType, ComplexFieldType]):
            if not specificEncodingType: yield encodingTypeClass
            else:
                for specificEncodingTypeClass in iterateClassAncestors(specificEncodingType, terminals=[PacketFieldType]):
                    yield (encodingTypeClass, specificEncodingTypeClass)
    
    @classmethod
    def RegisterTypeEncoder(cls, encodingType, encoder):
        # GetTypeKey is a generator. But the first key is the most specific. Use that to store.
        keyGenerator = cls._GetTypeKey(encodingType)
        cls.__TypeEncoders[next(keyGenerator)] = encoder
            
    @classmethod
    def GetTypeEncoder(cls, encodingType):
        for encodingKey in cls._GetTypeKey(encodingType):
            encoder = cls.__TypeEncoders.get(encodingKey, None)
            if encoder != None: return encoder
        return None
        
    def encode(self, stream, fieldType):
        typeEncoder = self.GetTypeEncoder(fieldType)
        if not typeEncoder:
            raise PacketEncodingError("Cannot encode fields of type {}".format(fieldType))
        typeEncoder().encode(EncoderStreamAdapter.Adapt(stream), fieldType, self)
        
    def decode(self, stream, fieldType):
        g = ReturnOrientedGenerator(self.decodeIterator(stream, fieldType))
        for waitingForStream in g: pass
        return g.result()
        
    def decodeIterator(self, stream, fieldType):
        typeDecoder = self.GetTypeEncoder(fieldType)
        if not typeDecoder:
            raise PacketEncodingError("Cannot decode fields of type {}".format(fieldType))
        yield from typeDecoder().decodeIterator(EncoderStreamAdapter.Adapt(stream), fieldType, self)


class IntrinsicTypeStandardEncoder:
    def _getPackCode(self, fieldType):
        raise Exception("Must be overridden in sub classes")
    
    def encode(self, stream, fieldType, topEncoder):
        packCode = self._getPackCode(fieldType)
        stream.pack(packCode, fieldType.data())
        
    def decodeIterator(self, stream, fieldType, topDecoder):
        packCode = self._getPackCode(fieldType)
        data = yield from stream.unpackIterator(packCode)
        fieldType.setData(data)    

class FloatEncoder(IntrinsicTypeStandardEncoder):
    # kept for symmetry with ints. But won't use much
    SIZE_TO_PACKCODE =  [
                    (32,"!f"),
                    (64,"!d")
                    ]
    
    SIZE_TO_PACKCODE_MAP = dict(SIZE_TO_PACKCODE)
    
    DEFAULT_BITS = 32
    
    def _getPackCode(self, fieldType):
        maxValue = PacketFieldType.GetAttribute(fieldType, Bits, self.DEFAULT_BITS)
        packCode = self.SIZE_TO_PACKCODE_MAP[maxValue]
        return packCode
PlaygroundStandardPacketEncoder.RegisterTypeEncoder(FloatFieldType, FloatEncoder)

class UintEncoder(IntrinsicTypeStandardEncoder):
    SIZE_TO_PACKCODE =  [
                        (2**8,"!B"),
                        (2**16,"!H"),
                        (2**32,"!I"),
                        (2**64,"!Q")
                        ]
                        
    DEFAULT_MAXVALUE = (2**32)-1
    
    def _getPackCode(self, fieldType):
        maxValue = PacketFieldType.GetAttribute(fieldType, MaxValue, self.DEFAULT_MAXVALUE)
        packCode = self._maxValueToPackCode(maxValue)
        return packCode
    
    def _maxValueToPackCode(self, maxValue):
        for packMaxValue, packCode in self.SIZE_TO_PACKCODE:
            if maxValue < packMaxValue: break 
        if maxValue >= packMaxValue:
            raise PacketEncodingError("Playground Standard Encoder cannot encode uint's of size {}.".format(maxValue))
        return packCode
PlaygroundStandardPacketEncoder.RegisterTypeEncoder(UINT, UintEncoder)

class IntEncoder(UintEncoder):
    SIZE_TO_PACKCODE =  [
                        (2**8,"!b"),
                        (2**16,"!h"),
                        (2**32,"!i"),
                        (2**64,"!q")
                        ]
PlaygroundStandardPacketEncoder.RegisterTypeEncoder(INT, IntEncoder)

class BoolEncoder(IntrinsicTypeStandardEncoder):
    def _getPackCode(self, fieldType):
        return "!?"
PlaygroundStandardPacketEncoder.RegisterTypeEncoder(BOOL, BoolEncoder)

class StringEncoder:
    STRING_LENGTH_BYTES = 2
    MAX_LENGTH = 2**(8*STRING_LENGTH_BYTES)
    UNICODE_ENCODING = "utf-8"
    
    STR_PACK_CODE = "!H{}s"
                        
    def encode(self, stream, strField, topEncoder):
        strLen = len(strField.data())
        if len(strField.data()) > self.MAX_LENGTH:
            raise PacketEncodingError("Playground Standard Encoder cannot encode string longer than {}".format(strLen))
        strEncoded = strField.data().encode(self.UNICODE_ENCODING)
        stream.pack(self.STR_PACK_CODE.format(strLen), strLen, strEncoded)
        
    def decodeIterator(self, stream, strField, topDecoder):
        strLen = yield from stream.unpackIterator("!H")
        strEncoded = yield from stream.unpackIterator("{}s".format(strLen))
        strDecoded = strEncoded.decode(self.UNICODE_ENCODING)
        strField.setData(strDecoded)
PlaygroundStandardPacketEncoder.RegisterTypeEncoder(StringFieldType, StringEncoder)

class BufferEncoder:
    BUFFER_LENGTH_BYTES = 8
    MAX_LENGTH = 2**(8*BUFFER_LENGTH_BYTES)
    
    BUF_PACK_CODE = "!Q{}s"
                        
    def encode(self, stream, bufField, topEncoder):
        bufLen = len(bufField.data())
        if bufLen > self.MAX_LENGTH:
            raise PacketEncodingError("Playground Standard Encoder cannot encode buffer longer than {}".format(bufLen))
        stream.pack(self.BUF_PACK_CODE.format(bufLen), bufLen, bufField.data())
        
    def decodeIterator(self, stream, bufField, topDecoder):
        bufLen = yield from stream.unpackIterator("!Q")
        bufData = yield from stream.unpackIterator("{}s".format(bufLen))
        bufField.setData(bufData)
PlaygroundStandardPacketEncoder.RegisterTypeEncoder(BufferFieldType, BufferEncoder)

class PacketFieldsEncoder:
    FIELD_TAG_PACK_CODE = "!H"
    FIELD_COUNT_PACK_CODE = "!H"
    
    def _processFields(self, fields):
        autoTag      = 0
        fieldToTag   = Bijection()

        for fieldName, fieldType in fields:
            if fieldName in fieldToTag:
                raise Exception("Duplicate Field")
            tag = PacketFieldType.GetAttribute(fieldType, ExplicitTag, None)
            if tag != None and tag in fieldToTag.inverse():
                raise Exception("Duplicate Explicit Tag")
            if tag == None:
                while autoTag in fieldToTag.inverse():
                    autoTag += 1
                tag = autoTag
            fieldToTag[fieldName] = tag
        return fieldToTag
    
    def encode(self, stream, complexType, topEncoder):
        packetFields = complexType.data()
        fieldToTag = self._processFields(packetFields.FIELDS)
        
        # Get all the fields that have data.
        encodeFields = []
        for fieldName, fieldType in packetFields.FIELDS:
            rawField = packetFields.__getrawfield__(fieldName)
            if rawField.data() == PacketFieldType.UNSET:
                if PacketFieldType.GetAttribute(rawField, Optional, False) == True:
                    continue
                else:
                    raise PacketEncodingError("Field '{}' is unset and not marked as optional.".format(fieldName))   
            encodeFields.append((fieldName, rawField))
            
        # Write the number of encoding fields into the stream
        stream.pack(self.FIELD_COUNT_PACK_CODE, len(encodeFields))
        
        # Write the actual fields into the stream
        for fieldName, rawField in encodeFields:
            try:
                tag = fieldToTag[fieldName]
                stream.pack(self.FIELD_TAG_PACK_CODE, tag)
                topEncoder.encode(stream, rawField)
            except Exception as encodingException:
                raise PacketEncodingError("Error encoding field {}.".format(fieldName)) from encodingException
    
    def decodeIterator(self, stream, complexType, topDecoder):
        # the complex type should have an unset inner data type. 
        # initialize this so it can be deserialized.
        complexType.initializeData()
        packetFields = complexType.data()
        fieldToTag = self._processFields(packetFields.FIELDS)
        fieldCount = yield from stream.unpackIterator(self.FIELD_COUNT_PACK_CODE)
        
        for i in range(fieldCount):
            fieldID = yield from stream.unpackIterator(self.FIELD_TAG_PACK_CODE)
            fieldName = fieldToTag.inverse()[fieldID]
            rawField  = packetFields.__getrawfield__(fieldName)
            try:
                yield from topDecoder.decodeIterator(stream, rawField)
            except Exception as encodingException:
                raise PacketEncodingError("Error decoding field {}.".format(fieldName)) from encodingException
PlaygroundStandardPacketEncoder.RegisterTypeEncoder(ComplexFieldType(PacketFields), PacketFieldsEncoder)

class ListEncoder:
    LIST_SIZE_PACK_CODE = "!H"
    
    def encode(self, stream, listType, topEncoder):
        stream.pack(self.LIST_SIZE_PACK_CODE, len(listType))
        for i in range(len(listType)):
            topEncoder.encode(stream, listType.__getrawitem__(i))
            
    def decodeIterator(self, stream, listType, topDecoder):
        listSize = yield from stream.unpackIterator(self.LIST_SIZE_PACK_CODE)
        listType.setData([]) # in case the size is 0
        for i in range(listSize):
            listType.append(PacketFieldType.UNSET) # Create a "null" entry in the list
            rawListData = listType.__getrawitem__(-1)
            try:
                yield from topDecoder.decodeIterator(stream, rawListData)
            except Exception as encodingException:
                raise PacketEncodingError("Error decoding index {} of list of type {}".format(i, listType.dataType()))
PlaygroundStandardPacketEncoder.RegisterTypeEncoder(ListFieldType(PacketFieldType), ListEncoder)
    
        
class PacketEncoder:
    PacketIdentifierTemplate = "!QQB{}sB{}s" # packet length, length checksum, definition, version, 
                                            # Definition and version are length-prefixed strings
                                           
    def encode(self, stream, complexType, topEncoder):
        packetStartPosition = stream.tell()
        
        packet = complexType.data()
        packetDefEncoded = packet.DEFINITION_IDENTIFIER.encode(UNICODE_ENCODING)
        packetVerEncoded = packet.DEFINITION_VERSION.encode(   UNICODE_ENCODING)
        packCode = self.PacketIdentifierTemplate.format(len(packetDefEncoded), len(packetVerEncoded))
        
        # start by putting in a 0 for the packet length
        stream.pack(packCode, 0, 0, len(packetDefEncoded), packetDefEncoded, 
                              len(packetVerEncoded), packetVerEncoded) 
                              
        PacketFieldsEncoder().encode(stream, complexType, topEncoder)
        
        packetEndPosition = stream.tell()
        packetLength = packetEndPosition - packetStartPosition
        
        stream.seek(packetStartPosition)
        # now write in the real value
        stream.pack("!Q", packetLength)
        # this isn't a real checksum. But probably good enough for most errors.
        stream.pack("!Q", packetLength^0xFFFFFFFFFFFFFFFF) 
        stream.seek(packetEndPosition)
        
    def decodeIterator(self, stream, complexType, topEncoder):
        packetLengthFound = False
        resync = False
        packetStartPosition = stream.tell()
        
        while not packetLengthFound:
        
            packetLength = yield from stream.unpackIterator("!Q")
            packetLengthCompare = yield from stream.unpackIterator("!Q")
            packetLengthCompare ^= 0xFFFFFFFFFFFFFFFF
            
            if packetLength != packetLengthCompare:
                if not resync:
                    logger.debug("Packet Length Mismatch {}!={} at stream pos {}. Advancing to resync".format(packetLength, packetLengthCompare, packetStartPosition))
                    resync = True
                packetStartPosition += 1
                stream.seek(packetStartPosition)
                #raise Exception("Packet Length Mismatch {}!={}".format(packetLength, packetLengthCompare))
            else:
                if resync:
                    logger.debug("Packet deserialization recovered at iterator position {}".format(packetStartPosition))
                    resync = False
                packetLengthFound = True
            
        stream.set_max_size(packetLength)
        
        nameLen = yield from stream.unpackIterator("!B")
        name    = yield from stream.unpackIterator("!{}s".format(nameLen))
        name    = name.decode(UNICODE_ENCODING)
        
        versionLen = yield from stream.unpackIterator("!B")
        version    = yield from stream.unpackIterator("!{}s".format(versionLen))
        version    = version.decode(UNICODE_ENCODING)
        
        version = PacketDefinitionVersion.FromString(version)

        basePacketType    = complexType.dataType()
        packetDefinitions = basePacketType.DEFINITIONS_STORE
        packetType = packetDefinitions.getDefinition(name, version)
        if not packetType:
            # uh oh. We don't have the definition of this packet. 
            # The encoder assumes an underlying "reliable" stream
            # so this should mean a misnamed packet, or a packet for
            # which we do not have a definition. So, we're going to skip
            # ahead until we can find something.
            pass # Do nothing. Let unreadBytes cleanup. TODO: logging/error
        else:
            packet = packetType()
            complexType.setData(packet)
            yield from PacketFieldsEncoder().decodeIterator(stream, complexType, topEncoder)
        bytesUsed = stream.tell() - packetStartPosition
        unreadBytes = packetLength - bytesUsed
        
        if unreadBytes:
            # uh oh. We have a mismatch between bytes read expected and actual.
            # read the remaining bytes.
            stream.read(unreadBytes)
            
            # raise an exception so that other parts of the system can identify
            # that we lost a packet. We've advanced the stream, so the system
            # should be able to pick up where it left off.
            raise Exception("Packet deserialization error. Packet Type={}, Expected Size={}, Actual Size={}".format(packetType, packetLength, bytesUsed))
        if not packetType:
            # We didn't have unused bytes, but we didn't have a packetType. Still an error
            raise Exception("Packet deserialization error. Packet Type={}".format(packetType))
PlaygroundStandardPacketEncoder.RegisterTypeEncoder(ComplexFieldType(NamedPacketType), PacketEncoder)
        
def basicUnitTest():
    import io
    
    uint1, uint2 = UINT(), UINT()
    int1, int2 = INT(), INT()
    bool1, bool2 = BOOL(), BOOL()
    stream = io.BytesIO()
    encoder = PlaygroundStandardPacketEncoder()
    
    uint1.setData(10)
    encoder.encode(stream, uint1)
    stream.seek(0)
    encoder.decode(stream, uint2)
    assert uint2.data() == uint1.data()
    
    stream = io.BytesIO()
    int1.setData(-10)
    encoder.encode(stream, int1)
    stream.seek(0)
    encoder.decode(stream, int2)
    assert int1.data() == int2.data()
    
    stream = io.BytesIO()
    bool1.setData(False)
    encoder.encode(stream, bool1)
    stream.seek(0)
    encoder.decode(stream, bool2)
    assert bool1.data() == bool2.data()
    
    listfield1 = ListFieldType(UINT)
    listfield2 = ListFieldType(UINT)
    listfield1.append(10)
    listfield1.append(100)
    listfield1.append(1000)
    
    stream = io.BytesIO()
    encoder.encode(stream, listfield1)
    stream.seek(0)
    encoder.decode(stream, listfield2)
    
    assert len(listfield1) == len(listfield2)
    for i in range(len(listfield1)):
        assert listfield1[i] == listfield2[i]
    
    str1 = StringFieldType()
    str2 = StringFieldType()
    str1.setData("Test1 string")
    
    stream = io.BytesIO()
    encoder.encode(stream, str1)
    stream.seek(0)
    encoder.decode(stream, str2)
    
    assert str1.data() == str2.data()
    
    class SomeFields(PacketFields):
        FIELDS = [  ("field1", UINT({Bits:32})),
                    ("field2", UINT({Bits:32})),
                    ("list1",  ListFieldType(UINT({Bits:8})))
                    ]
    
    fields1Field = ComplexFieldType(SomeFields)
    fields2Field = ComplexFieldType(SomeFields)
    
    fields1 = SomeFields()
    fields1.field1 = 50
    fields1.field2 = 500
    fields1.list1 = []
    fields1.list1.append(0)
    fields1.list1.append(255)
    
    fields1Field.setData(fields1)
    fields2Field.setData(SomeFields())
    
    stream = io.BytesIO()
    encoder.encode(stream, fields1Field)
    stream.seek(0)
    encoder.decode(stream, fields2Field)
    
    fields2 = fields2Field.data()
    
    assert fields1.field1 == fields2.field1
    assert fields1.field2 == fields2.field2
    assert len(fields1.list1) == len(fields2.list1)
    assert fields1.list1[0] == fields2.list1[0]
    assert fields1.list1[-1] == fields2.list1[-1]
    
    # Packet not tested in this file. See basicUnitTest in PacketType.py
    
if __name__=="__main__":
    basicUnitTest()