import sqlite3
import time
from contextlib import closing
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import audio

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
WHATSAPP_DB_PATH = os.path.join(os.path.dirname(MESSAGES_DB_PATH), 'whatsapp.db')
WHATSAPP_API_BASE_URL = "http://localhost:8080/api"
BRIDGE_HEADERS = {"Authorization": f"Bearer {os.environ.get('BRIDGE_API_TOKEN', '')}"}

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

_NAME_CACHE_SECONDS = 600
_name_cache: dict = {}


def _wa_connect():
    """Read-only connection to whatsmeow's own store, which holds contact names."""
    return sqlite3.connect(f"file:{WHATSAPP_DB_PATH}?mode=ro", uri=True, timeout=1)


def _user_part(jid: str) -> str:
    return jid.split('@')[0].split(':')[0]


def _best_name(rows) -> Optional[str]:
    """Pick one name from (full_name, first_name, push_name, business_name) rows.

    Saved address-book names win. A name someone chose for themselves is marked
    with '~', the way WhatsApp's own app marks it.
    """
    for column in (0, 1):
        for row in rows:
            if row[column]:
                return row[column]
    for row in rows:
        if row[2]:
            return "~" + row[2]
    for row in rows:
        if row[3]:
            return row[3]
    return None


def _counterpart_jids(conn, user: str) -> set:
    """A user's phone-number and LID forms, since contacts may be keyed by either."""
    jids = {f"{user}@s.whatsapp.net", f"{user}@lid"}
    for (pn,) in conn.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (user,)):
        jids.add(f"{pn}@s.whatsapp.net")
    for (lid,) in conn.execute("SELECT lid FROM whatsmeow_lid_map WHERE pn = ?", (user,)):
        jids.add(f"{lid}@lid")
    return jids


def lookup_contact_name(sender: str) -> Optional[str]:
    """Human name for a WhatsApp user (a phone number, LID or JID), or None."""
    user = _user_part(sender or "")
    if not user:
        return None
    cached = _name_cache.get(user)
    if cached and time.time() - cached[0] < _NAME_CACHE_SECONDS:
        return cached[1]
    try:
        with closing(_wa_connect()) as conn:
            jids = sorted(_counterpart_jids(conn, user))
            marks = ",".join("?" * len(jids))
            rows = conn.execute(
                "SELECT full_name, first_name, push_name, business_name "
                f"FROM whatsmeow_contacts WHERE their_jid IN ({marks})",
                jids,
            ).fetchall()
    except sqlite3.Error:
        return None
    name = _best_name(rows)
    _name_cache[user] = (time.time(), name)
    return name


def _is_unnamed(jid: str, name: Optional[str]) -> bool:
    return not name or name == _user_part(jid) or name.lstrip('+').isdigit()


def resolve_chat_name(jid: str, name: Optional[str]) -> Optional[str]:
    """The chat's stored name, replaced by a contact name when it is only a number."""
    if jid.endswith('@g.us') or not _is_unnamed(jid, name):
        return name
    return lookup_contact_name(jid) or name


def display_sender(sender: Optional[str]) -> Optional[str]:
    """A sender shown by name when we know it, otherwise as stored."""
    if not sender:
        return sender
    return lookup_contact_name(sender) or sender


def phone_for_jid(jid: str) -> str:
    """The phone number behind a JID; for an anonymized LID, look the number up."""
    user = _user_part(jid)
    if not jid.endswith('@lid'):
        return user
    try:
        with closing(_wa_connect()) as conn:
            row = conn.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (user,)).fetchone()
    except sqlite3.Error:
        return user
    return row[0] if row else user


def _name_matched_jids(query: str) -> set:
    """JIDs, in both phone and LID form, of contacts whose name contains the query."""
    pattern = f"%{query}%"
    try:
        with closing(_wa_connect()) as conn:
            rows = conn.execute(
                "SELECT their_jid FROM whatsmeow_contacts WHERE "
                "LOWER(full_name) LIKE LOWER(?) OR LOWER(first_name) LIKE LOWER(?) OR "
                "LOWER(push_name) LIKE LOWER(?) OR LOWER(business_name) LIKE LOWER(?) LIMIT 200",
                (pattern, pattern, pattern, pattern),
            ).fetchall()
            jids: set = set()
            for (their_jid,) in rows:
                jids.add(their_jid)
                jids |= _counterpart_jids(conn, _user_part(their_jid))
            return jids
    except sqlite3.Error:
        return set()


def _contacts_matching(query: str) -> list:
    """Saved contacts whose name or number matches, as (jid, phone_number, name, all_jids).

    The JID is the phone-number form when we know it, since that is what
    send_message accepts. all_jids holds every form of the contact, so callers
    can tell when a chat for them already exists.
    """
    pattern = f"%{query}%"
    try:
        with closing(_wa_connect()) as conn:
            rows = conn.execute(
                "SELECT their_jid, full_name, first_name, push_name, business_name "
                "FROM whatsmeow_contacts WHERE "
                "LOWER(full_name) LIKE LOWER(?) OR LOWER(first_name) LIKE LOWER(?) OR "
                "LOWER(push_name) LIKE LOWER(?) OR LOWER(business_name) LIKE LOWER(?) OR "
                "their_jid LIKE ? LIMIT 200",
                (pattern, pattern, pattern, pattern, pattern),
            ).fetchall()
            found = []
            for their_jid, full, first, push, business in rows:
                name = _best_name([(full, first, push, business)])
                if not name:
                    continue
                user = _user_part(their_jid)
                jid, phone = their_jid, user
                if their_jid.endswith('@lid'):
                    row = conn.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (user,)).fetchone()
                    if row:
                        jid, phone = f"{row[0]}@s.whatsapp.net", row[0]
                found.append((jid, phone, name, _counterpart_jids(conn, user) | {their_jid}))
            return found
    except sqlite3.Error:
        return []


def get_sender_name(sender_jid: str) -> str:
    named = lookup_contact_name(sender_jid)
    if named:
        return named
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # First try matching by exact JID
        cursor.execute("""
            SELECT name
            FROM chats
            WHERE jid = ?
            LIMIT 1
        """, (sender_jid,))
        
        result = cursor.fetchone()
        
        # If no result, try looking for the number within JIDs
        if not result:
            # Extract the phone number part if it's a JID
            if '@' in sender_jid:
                phone_part = sender_jid.split('@')[0]
            else:
                phone_part = sender_jid
                
            cursor.execute("""
                SELECT name
                FROM chats
                WHERE jid LIKE ? AND jid NOT LIKE '%@g.us'
                LIMIT 1
            """, (f"%{phone_part}%",))
            
            result = cursor.fetchone()
        
        if result and result[0]:
            return result[0]
        else:
            return sender_jid
        
    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()

def get_reactions_text(chat_jid: str, message_id: str) -> str:
    """Reactions on a message as one line, e.g. '👍 Alice, Bob; ❤️ Me'. Empty if none."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT emoji, sender, is_from_me FROM reactions "
            "WHERE chat_jid = ? AND message_id = ? ORDER BY timestamp",
            (chat_jid, message_id),
        )
        rows = cursor.fetchall()
    except sqlite3.Error:
        # A database from before reactions were captured has no such table yet.
        return ""
    finally:
        if 'conn' in locals():
            conn.close()

    names_by_emoji: dict = {}
    for emoji, sender, is_from_me in rows:
        name = "Me" if is_from_me else get_sender_name(sender)
        names_by_emoji.setdefault(emoji, []).append(name)
    return "; ".join(f"{emoji} {', '.join(names)}" for emoji, names in names_by_emoji.items())

def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    
    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")

    reactions = get_reactions_text(message.chat_jid, message.id)
    if reactions:
        output += f"    Reactions: {reactions}\n"
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output
    
    for message in messages:
        output += format_message(message, show_chat_info)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        
        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            where_clauses.append("messages.sender = ?")
            params.append(sender_phone_number)
            
        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            )
            result.append(message)
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
            
        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8]
        )
        
        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))
        
        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))
        
        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["""
            SELECT 
                chats.jid,
                chats.name,
                chats.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats
        """]
        
        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid 
                AND chats.last_message_time = messages.timestamp
            """)
            
        where_clauses = []
        params = []
        
        if query:
            by_contact = sorted(_name_matched_jids(query))
            clause = "LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?"
            params.extend([f"%{query}%", f"%{query}%"])
            if by_contact:
                clause += f" OR chats.jid IN ({','.join('?' * len(by_contact))})"
                params.extend(by_contact)
            where_clauses.append(f"({clause})")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=resolve_chat_name(chat_data[0], chat_data[1]),
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=display_sender(chat_data[4]),
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Split query into characters to support partial matching
        search_pattern = '%' +query + '%'
        
        # Contacts are also matched by the names in whatsmeow's contact store, so a
        # chat that is only stored under a number can still be found by name.
        by_contact = sorted(_name_matched_jids(query))
        match = "LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?)"
        params = [search_pattern, search_pattern]
        if by_contact:
            match += f" OR jid IN ({','.join('?' * len(by_contact))})"
            params.extend(by_contact)

        cursor.execute(f"""
            SELECT DISTINCT
                jid,
                name
            FROM chats
            WHERE
                ({match})
                AND jid NOT LIKE '%@g.us'
            ORDER BY name, jid
            LIMIT 50
        """, params)

        contacts = cursor.fetchall()

        result = []
        for contact_data in contacts:
            contact = Contact(
                phone_number=phone_for_jid(contact_data[0]),
                name=resolve_chat_name(contact_data[0], contact_data[1]),
                jid=contact_data[0]
            )
            result.append(contact)
            
        # Saved contacts you have no chat with (for example people who only appear in
        # groups) are returned too, with a phone-number JID that send_message accepts.
        chat_jids = {c.jid for c in result}
        listed = set(chat_jids)
        extra = []
        for jid, phone, name, all_jids in _contacts_matching(query):
            if all_jids & chat_jids or jid in listed:
                continue
            listed.add(jid)
            extra.append(Contact(phone_number=phone, name=name, jid=jid))
        extra.sort(key=lambda c: (c.name or "").lower())
        result.extend(extra[:max(0, 50 - len(result))])

        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, (jid, jid, limit, page * limit))
        
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=resolve_chat_name(chat_data[0], chat_data[1]),
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=display_sender(chat_data[4]),
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, (jid, jid))
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        
        return format_message(message)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        query = """
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """
        
        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            """
            
        query += " WHERE c.jid = ?"
        
        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=resolve_chat_name(chat_data[0], chat_data[1]),
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=display_sender(chat_data[4]),
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
            LIMIT 1
        """, (f"%{sender_phone_number}%",))
        
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=resolve_chat_name(chat_data[0], chat_data[1]),
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=display_sender(chat_data[4]),
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload, headers=BRIDGE_HEADERS)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload, headers=BRIDGE_HEADERS)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload, headers=BRIDGE_HEADERS)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload, headers=BRIDGE_HEADERS)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None
