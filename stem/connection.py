"""
Functions for connecting and authenticating to the tor process.

get_protocolinfo_by_port - PROTOCOLINFO query via a control port.
get_protocolinfo_by_socket - PROTOCOLINFO query via a control socket.
ProtocolInfoResponse - Reply from a PROTOCOLINFO query.
  |- Attributes:
  |  |- protocol_version
  |  |- tor_version
  |  |- auth_methods
  |  |- unknown_auth_methods
  |  |- cookie_path
  |  +- socket
  +- convert - parses a ControlMessage, turning it into a ProtocolInfoResponse
"""

import os
import logging
import binascii

import stem.socket
import stem.version
import stem.util.enum
import stem.util.system

LOGGER = logging.getLogger("stem")

# Methods by which a controller can authenticate to the control port. Tor gives
# a list of all the authentication methods it will accept in response to
# PROTOCOLINFO queries.
#
# NONE     - No authentication required
# PASSWORD - See tor's HashedControlPassword option. Controllers must provide
#            the password used to generate the hash.
# COOKIE   - See tor's CookieAuthentication option. Controllers need to supply
#            the contents of the cookie file.
# UNKNOWN  - Tor provided one or more authentication methods that we don't
#            recognize. This is probably from a new addition to the control
#            protocol.

AuthMethod = stem.util.enum.Enum("NONE", "PASSWORD", "COOKIE", "UNKNOWN")

AUTH_COOKIE_MISSING = "Authentication failed: '%s' doesn't exist"
AUTH_COOKIE_WRONG_SIZE = "Authentication failed: authentication cookie '%s' is the wrong size (%i bytes instead of 32)"

def authenticate_none(control_socket):
  """
  Authenticates to an open control socket. All control connections need to
  authenticate before they can be used, even if tor hasn't been configured to
  use any authentication.
  
  If authentication fails then tor will close the control socket.
  
  Arguments:
    control_socket (stem.socket.ControlSocket) - socket to be authenticated
  
  Raises:
    ValueError if the empty authentication credentials aren't accepted
    stem.socket.ProtocolError the content from the socket is malformed
    stem.socket.SocketError if problems arise in using the socket
  """
  
  control_socket.send("AUTHENTICATE")
  auth_response = control_socket.recv()
  
  # if we got anything but an OK response then error
  if str(auth_response) != "OK":
    raise ValueError(str(auth_response))

def authenticate_password(control_socket, password):
  """
  Authenticates to a control socket that uses a password (via the
  HashedControlPassword torrc option). Quotes in the password are escaped.
  
  If authentication fails then tor will close the control socket.
  
  Arguments:
    control_socket (stem.socket.ControlSocket) - socket to be authenticated
    password (str) - passphrase to present to the socket
  
  Raises:
    ValueError if the authentication credentials aren't accepted
    stem.socket.ProtocolError the content from the socket is malformed
    stem.socket.SocketError if problems arise in using the socket
  """
  
  # Escapes quotes. Tor can include those in the password hash, in which case
  # it expects escaped quotes from the controller. For more information see...
  # https://trac.torproject.org/projects/tor/ticket/4600
  
  password = password.replace('"', '\\"')
  
  control_socket.send("AUTHENTICATE \"%s\"" % password)
  auth_response = control_socket.recv()
  
  # if we got anything but an OK response then error
  if str(auth_response) != "OK":
    raise ValueError(str(auth_response))

def authenticate_cookie(control_socket, cookie_path):
  """
  Authenticates to a control socket that uses the contents of an authentication
  cookie (generated via the CookieAuthentication torrc option). This does basic
  validation that this is a cookie before presenting the contents to the
  socket.
  
  If authentication fails then tor will close the control socket.
  
  Arguments:
    control_socket (stem.socket.ControlSocket) - socket to be authenticated
    cookie_path (str) - path of the authentication cookie to send to tor
  
  Raises:
    ValueError if the authentication credentials aren't accepted
    OSError if the cookie file doesn't exist or we're unable to read it
    stem.socket.ProtocolError the content from the socket is malformed
    stem.socket.SocketError if problems arise in using the socket
  """
  
  if not os.path.exists(cookie_path):
    raise OSError(AUTH_COOKIE_MISSING % cookie_path)
  
  # Abort if the file isn't 32 bytes long. This is to avoid exposing arbitrary
  # file content to the port.
  #
  # Without this a malicious socket could, for instance, claim that
  # '~/.bash_history' or '~/.ssh/id_rsa' was its authentication cookie to trick
  # us into reading it for them with our current permissions.
  #
  # https://trac.torproject.org/projects/tor/ticket/4303
  
  auth_cookie_size = os.path.getsize(cookie_path)
  
  if auth_cookie_size != 32:
    raise ValueError(AUTH_COOKIE_WRONG_SIZE % (cookie_path, auth_cookie_size))
  
  try:
    auth_cookie_file = open(cookie_path, "r")
    auth_cookie_contents = auth_cookie_file.read()
    auth_cookie_file.close()
    
    control_socket.send("AUTHENTICATE %s" % binascii.b2a_hex(auth_cookie_contents))
    auth_response = control_socket.recv()
    
    # if we got anything but an OK response then error
    if str(auth_response) != "OK":
      raise ValueError(str(auth_response))
  except IOError, exc:
    raise OSError(exc)

def get_protocolinfo_by_port(control_addr = "127.0.0.1", control_port = 9051, get_socket = False):
  """
  Issues a PROTOCOLINFO query to a control port, getting information about the
  tor process running on it.
  
  Arguments:
    control_addr (str) - ip address of the controller
    control_port (int) - port number of the controller
    get_socket (bool)  - provides the socket with the response if True,
                         otherwise the socket is closed when we're done
  
  Returns:
    stem.connection.ProtocolInfoResponse provided by tor, if get_socket is True
    then this provides a tuple instead with both the response and connected
    socket (stem.socket.ControlPort)
  
  Raises:
    stem.socket.ProtocolError if the PROTOCOLINFO response is malformed
    stem.socket.SocketError if problems arise in establishing or using the
      socket
  """
  
  try:
    control_socket = stem.socket.ControlPort(control_addr, control_port)
    control_socket.connect()
    control_socket.send("PROTOCOLINFO 1")
    protocolinfo_response = control_socket.recv()
    ProtocolInfoResponse.convert(protocolinfo_response)
    
    # attempt to expand relative cookie paths using our port to infer the pid
    if control_addr == "127.0.0.1":
      _expand_cookie_path(protocolinfo_response, stem.util.system.get_pid_by_port, control_port)
    
    if get_socket:
      return (protocolinfo_response, control_socket)
    else:
      control_socket.close()
      return protocolinfo_response
  except stem.socket.ControllerError, exc:
    control_socket.close()
    raise exc

def get_protocolinfo_by_socket(socket_path = "/var/run/tor/control", get_socket = False):
  """
  Issues a PROTOCOLINFO query to a control socket, getting information about
  the tor process running on it.
  
  Arguments:
    socket_path (str) - path where the control socket is located
    get_socket (bool) - provides the socket with the response if True,
                        otherwise the socket is closed when we're done
  
  Returns:
    stem.connection.ProtocolInfoResponse provided by tor, if get_socket is True
    then this provides a tuple instead with both the response and connected
    socket (stem.socket.ControlSocketFile)
  
  Raises:
    stem.socket.ProtocolError if the PROTOCOLINFO response is malformed
    stem.socket.SocketError if problems arise in establishing or using the
      socket
  """
  
  try:
    control_socket = stem.socket.ControlSocketFile(socket_path)
    control_socket.connect()
    control_socket.send("PROTOCOLINFO 1")
    protocolinfo_response = control_socket.recv()
    ProtocolInfoResponse.convert(protocolinfo_response)
    
    # attempt to expand relative cookie paths using our port to infer the pid
    _expand_cookie_path(protocolinfo_response, stem.util.system.get_pid_by_open_file, socket_path)
    
    if get_socket:
      return (protocolinfo_response, control_socket)
    else:
      control_socket.close()
      return protocolinfo_response
  except stem.socket.ControllerError, exc:
    control_socket.close()
    raise exc

def _expand_cookie_path(protocolinfo_response, pid_resolver, pid_resolution_arg):
  """
  Attempts to expand a relative cookie path with the given pid resolver. This
  leaves the cookie_path alone if it's already absolute, None, or the system
  calls fail.
  """
  
  cookie_path = protocolinfo_response.cookie_path
  if cookie_path and stem.util.system.is_relative_path(cookie_path):
    try:
      tor_pid = pid_resolver(pid_resolution_arg)
      if not tor_pid: raise IOError("pid lookup failed")
      
      tor_cwd = stem.util.system.get_cwd(tor_pid)
      if not tor_cwd: raise IOError("cwd lookup failed")
      
      cookie_path = stem.util.system.expand_path(cookie_path, tor_cwd)
    except IOError, exc:
      resolver_labels = {
        stem.util.system.get_pid_by_name: " by name",
        stem.util.system.get_pid_by_port: " by port",
        stem.util.system.get_pid_by_open_file: " by socket file",
      }
      
      pid_resolver_label = resolver_labels.get(pid_resolver, "")
      LOGGER.debug("unable to expand relative tor cookie path%s: %s" % (pid_resolver_label, exc))
  
  protocolinfo_response.cookie_path = cookie_path

class ProtocolInfoResponse(stem.socket.ControlMessage):
  """
  Version one PROTOCOLINFO query response.
  
  According to the control spec the cookie_file is an absolute path. However,
  this often is not the case (especially for the Tor Browser Bundle)...
  https://trac.torproject.org/projects/tor/ticket/1101
  
  If the path is relative then we'll make an attempt (which may not work) to
  correct this.
  
  The protocol_version is the only mandatory data for a valid PROTOCOLINFO
  response, so all other values are None if undefined or empty if a collection.
  
  Attributes:
    protocol_version (int)             - protocol version of the response
    tor_version (stem.version.Version) - version of the tor process
    auth_methods (tuple)               - AuthMethod types that tor will accept
    unknown_auth_methods (tuple)       - strings of unrecognized auth methods
    cookie_path (str)                  - path of tor's authentication cookie
  """
  
  def convert(control_message):
    """
    Parses a ControlMessage, performing an in-place conversion of it into a
    ProtocolInfoResponse.
    
    Arguments:
      control_message (stem.socket.ControlMessage) -
        message to be parsed as a PROTOCOLINFO reply
    
    Raises:
      stem.socket.ProtocolError the message isn't a proper PROTOCOLINFO response
      TypeError if argument isn't a ControlMessage
    """
    
    if isinstance(control_message, stem.socket.ControlMessage):
      control_message.__class__ = ProtocolInfoResponse
      control_message._parse_message()
      return control_message
    else:
      raise TypeError("Only able to convert stem.socket.ControlMessage instances")
  
  convert = staticmethod(convert)
  
  def _parse_message(self):
    # Example:
    #   250-PROTOCOLINFO 1
    #   250-AUTH METHODS=COOKIE COOKIEFILE="/home/atagar/.tor/control_auth_cookie"
    #   250-VERSION Tor="0.2.1.30"
    #   250 OK
    
    self.protocol_version = None
    self.tor_version = None
    self.cookie_path = None
    
    auth_methods, unknown_auth_methods = [], []
    
    # sanity check that we're a PROTOCOLINFO response
    if not list(self)[0].startswith("PROTOCOLINFO"):
      msg = "Message is not a PROTOCOLINFO response"
      raise stem.socket.ProtocolError(msg)
    
    for line in self:
      if line == "OK": break
      elif line.is_empty(): continue # blank line
      
      line_type = line.pop()
      
      if line_type == "PROTOCOLINFO":
        # Line format:
        #   FirstLine = "PROTOCOLINFO" SP PIVERSION CRLF
        #   PIVERSION = 1*DIGIT
        
        if line.is_empty():
          msg = "PROTOCOLINFO response's initial line is missing the protocol version: %s" % line
          raise stem.socket.ProtocolError(msg)
        
        piversion = line.pop()
        
        if not piversion.isdigit():
          msg = "PROTOCOLINFO response version is non-numeric: %s" % line
          raise stem.socket.ProtocolError(msg)
        
        self.protocol_version = int(piversion)
        
        # The piversion really should be "1" but, according to the spec, tor
        # does not necessarily need to provide the PROTOCOLINFO version that we
        # requested. Log if it's something we aren't expecting but still make
        # an effort to parse like a v1 response.
        
        if self.protocol_version != 1:
          LOGGER.warn("We made a PROTOCOLINFO v1 query but got a version %i response instead. We'll still try to use it, but this may cause problems." % self.protocol_version)
      elif line_type == "AUTH":
        # Line format:
        #   AuthLine = "250-AUTH" SP "METHODS=" AuthMethod *("," AuthMethod)
        #              *(SP "COOKIEFILE=" AuthCookieFile) CRLF
        #   AuthMethod = "NULL" / "HASHEDPASSWORD" / "COOKIE"
        #   AuthCookieFile = QuotedString
        
        # parse AuthMethod mapping
        if not line.is_next_mapping("METHODS"):
          msg = "PROTOCOLINFO response's AUTH line is missing its mandatory 'METHODS' mapping: %s" % line
          raise stem.socket.ProtocolError(msg)
        
        for method in line.pop_mapping()[1].split(","):
          if method == "NULL":
            auth_methods.append(AuthMethod.NONE)
          elif method == "HASHEDPASSWORD":
            auth_methods.append(AuthMethod.PASSWORD)
          elif method == "COOKIE":
            auth_methods.append(AuthMethod.COOKIE)
          else:
            unknown_auth_methods.append(method)
            LOGGER.info("PROTOCOLINFO response had an unrecognized authentication method: %s" % method)
            
            # our auth_methods should have a single AuthMethod.UNKNOWN entry if
            # any unknown authentication methods exist
            if not AuthMethod.UNKNOWN in auth_methods:
              auth_methods.append(AuthMethod.UNKNOWN)
        
        # parse optional COOKIEFILE mapping (quoted and can have escapes)
        if line.is_next_mapping("COOKIEFILE", True, True):
          self.cookie_path = line.pop_mapping(True, True)[1]
          
          # attempt to expand relative cookie paths
          _expand_cookie_path(self, stem.util.system.get_pid_by_name, "tor")
      elif line_type == "VERSION":
        # Line format:
        #   VersionLine = "250-VERSION" SP "Tor=" TorVersion OptArguments CRLF
        #   TorVersion = QuotedString
        
        if not line.is_next_mapping("Tor", True):
          msg = "PROTOCOLINFO response's VERSION line is missing its mandatory tor version mapping: %s" % line
          raise stem.socket.ProtocolError(msg)
        
        torversion = line.pop_mapping(True)[1]
        
        try:
          self.tor_version = stem.version.Version(torversion)
        except ValueError, exc:
          raise stem.socket.ProtocolError(exc)
      else:
        LOGGER.debug("unrecognized PROTOCOLINFO line type '%s', ignoring entry: %s" % (line_type, line))
    
    self.auth_methods = tuple(auth_methods)
    self.unknown_auth_methods = tuple(unknown_auth_methods)

