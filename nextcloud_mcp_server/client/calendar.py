"""CalDAV client for NextCloud calendar operations."""

import xml.etree.ElementTree as ET
from datetime import datetime, date
from typing import Dict, Any, List, Optional, Tuple
import logging
from httpx import HTTPStatusError
from icalendar import Calendar, Event as ICalEvent, vRecur, Alarm
from datetime import timedelta
import uuid

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)


class CalendarClient(BaseNextcloudClient):
    """Client for NextCloud CalDAV calendar operations."""

    def _get_caldav_base_path(self) -> str:
        """Helper to get the base CalDAV path for calendars."""
        return f"/remote.php/dav/calendars/{self.username}"

    def _get_principals_path(self) -> str:
        """Helper to get the principals path for the user."""
        return f"/remote.php/dav/principals/users/{self.username}"

    async def list_calendars(self) -> List[Dict[str, Any]]:
        """List all available calendars for the user."""
        caldav_path = self._get_caldav_base_path()

        propfind_body = """<?xml version="1.0" encoding="utf-8"?>
        <d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/">
            <d:prop>
                <d:displayname/>
                <d:resourcetype/>
                <c:calendar-description/>
                <cs:calendar-color/>
                <c:supported-calendar-component-set/>
            </d:prop>
        </d:propfind>"""

        headers = {
            "Depth": "1",
            "Content-Type": "application/xml",
            "Accept": "application/xml",
        }

        try:
            response = await self._client.request(
                "PROPFIND", caldav_path, content=propfind_body, headers=headers
            )
            response.raise_for_status()

            # Parse XML response
            root = ET.fromstring(response.content)
            calendars = []

            for response_elem in root.findall(".//{DAV:}response"):
                href = response_elem.find(".//{DAV:}href")
                if href is None:
                    continue

                href_text = href.text or ""
                if not href_text.endswith("/"):
                    continue  # Skip non-calendar resources

                # Extract calendar name from href
                calendar_name = href_text.rstrip("/").split("/")[-1]
                if not calendar_name or calendar_name == self.username:
                    continue

                # Get properties
                propstat = response_elem.find(".//{DAV:}propstat")
                if propstat is None:
                    continue

                prop = propstat.find(".//{DAV:}prop")
                if prop is None:
                    continue

                # Check if it's a calendar resource
                resourcetype = prop.find(".//{DAV:}resourcetype")
                is_calendar = (
                    resourcetype is not None
                    and resourcetype.find(".//{urn:ietf:params:xml:ns:caldav}calendar")
                    is not None
                )

                if not is_calendar:
                    continue

                # Extract calendar properties
                displayname_elem = prop.find(".//{DAV:}displayname")
                displayname = (
                    displayname_elem.text
                    if displayname_elem is not None
                    else calendar_name
                )

                description_elem = prop.find(
                    ".//{urn:ietf:params:xml:ns:caldav}calendar-description"
                )
                description = (
                    description_elem.text if description_elem is not None else ""
                )

                color_elem = prop.find(
                    ".//{http://calendarserver.org/ns/}calendar-color"
                )
                color = color_elem.text if color_elem is not None else "#1976D2"

                calendars.append(
                    {
                        "name": calendar_name,
                        "display_name": displayname,
                        "description": description,
                        "color": color,
                        "href": href_text,
                    }
                )

            logger.debug(f"Found {len(calendars)} calendars")
            return calendars

        except HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.warning(
                    "Authentication failed for CalDAV - Calendar app may not be enabled for this user"
                )
                return []
            elif e.response.status_code == 404:
                logger.warning(
                    "CalDAV endpoint not found - Calendar app may not be installed"
                )
                return []
            logger.error(f"HTTP error listing calendars: {e}")
            raise e
        except Exception as e:
            logger.error(f"Unexpected error listing calendars: {e}")
            raise e

    async def get_calendar_events(
        self,
        calendar_name: str,
        start_date: str = "",
        end_date: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List events in a calendar within date range."""
        calendar_path = f"{self._get_caldav_base_path()}/{calendar_name}/"

        # Build time range filter if dates provided
        time_range_filter = ""
        if start_date or end_date:
            start_dt = start_date or "19700101T000000Z"
            end_dt = end_date or "20301231T235959Z"
            time_range_filter = f"""
                <c:time-range start="{start_dt}" end="{end_dt}"/>
            """

        report_body = f"""<?xml version="1.0" encoding="utf-8"?>
        <c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
            <d:prop>
                <d:getetag/>
                <c:calendar-data/>
            </d:prop>
            <c:filter>
                <c:comp-filter name="VCALENDAR">
                    <c:comp-filter name="VEVENT">
                        {time_range_filter}
                    </c:comp-filter>
                </c:comp-filter>
            </c:filter>
        </c:calendar-query>"""

        headers = {
            "Depth": "1",
            "Content-Type": "application/xml",
            "Accept": "application/xml",
        }

        try:
            response = await self._client.request(
                "REPORT", calendar_path, content=report_body, headers=headers
            )
            response.raise_for_status()

            # Parse XML response and extract events
            root = ET.fromstring(response.content)
            events = []

            for response_elem in root.findall(".//{DAV:}response"):
                href = response_elem.find(".//{DAV:}href")
                if href is None:
                    continue

                propstat = response_elem.find(".//{DAV:}propstat")
                if propstat is None:
                    continue

                prop = propstat.find(".//{DAV:}prop")
                if prop is None:
                    continue

                calendar_data = prop.find(
                    ".//{urn:ietf:params:xml:ns:caldav}calendar-data"
                )
                etag_elem = prop.find(".//{DAV:}getetag")

                if calendar_data is not None and calendar_data.text:
                    event_data = self._parse_ical_event(calendar_data.text)
                    if event_data:
                        event_data["href"] = href.text
                        event_data["etag"] = (
                            etag_elem.text if etag_elem is not None else ""
                        )
                        events.append(event_data)

                if len(events) >= limit:
                    break

            logger.debug(f"Found {len(events)} events")
            return events

        except HTTPStatusError as e:
            logger.error(f"HTTP error getting calendar events: {e}")
            raise e
        except Exception as e:
            logger.error(f"Unexpected error getting calendar events: {e}")
            raise e

    async def create_event(
        self, calendar_name: str, event_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create a new calendar event with comprehensive features."""
        event_uid = str(uuid.uuid4())
        event_filename = f"{event_uid}.ics"
        event_path = f"{self._get_caldav_base_path()}/{calendar_name}/{event_filename}"

        # Create iCalendar event
        ical_content = self._create_ical_event(event_data, event_uid)

        headers = {
            "Content-Type": "text/calendar; charset=utf-8",
            "If-None-Match": "*",  # Ensure we're creating, not updating
        }

        try:
            response = await self._client.put(
                event_path, content=ical_content, headers=headers
            )
            response.raise_for_status()

            logger.debug(f"Created event {event_uid}")
            return {
                "uid": event_uid,
                "href": event_path,
                "etag": response.headers.get("etag", ""),
                "status_code": response.status_code,
            }

        except HTTPStatusError as e:
            logger.error(f"HTTP error creating event: {e}")
            raise e
        except Exception as e:
            logger.error(f"Unexpected error creating event: {e}")
            raise e

    async def update_event(
        self,
        calendar_name: str,
        event_uid: str,
        event_data: Dict[str, Any],
        etag: str = "",
    ) -> Dict[str, Any]:
        """Update an existing calendar event."""
        event_filename = f"{event_uid}.ics"
        event_path = f"{self._get_caldav_base_path()}/{calendar_name}/{event_filename}"

        # Get existing event data to merge with updates
        existing_event_data = {}
        if not etag:
            try:
                existing_event_data, current_etag = await self.get_event(
                    calendar_name, event_uid
                )
                etag = current_etag
            except Exception:
                # Continue without etag if we can't get it
                pass

        # Merge existing data with new data (new data takes precedence)
        merged_data = {**existing_event_data, **event_data}

        # Create updated iCalendar event
        ical_content = self._create_ical_event(merged_data, event_uid)

        headers = {
            "Content-Type": "text/calendar; charset=utf-8",
        }
        if etag:
            headers["If-Match"] = etag

        try:
            response = await self._client.put(
                event_path, content=ical_content, headers=headers
            )
            response.raise_for_status()

            logger.debug(f"Updated event {event_uid}")
            return {
                "uid": event_uid,
                "href": event_path,
                "etag": response.headers.get("etag", ""),
                "status_code": response.status_code,
            }

        except HTTPStatusError as e:
            logger.error(f"HTTP error updating event: {e}")
            raise e
        except Exception as e:
            logger.error(f"Unexpected error updating event: {e}")
            raise e

    async def delete_event(self, calendar_name: str, event_uid: str) -> Dict[str, Any]:
        """Delete a calendar event."""
        event_filename = f"{event_uid}.ics"
        event_path = f"{self._get_caldav_base_path()}/{calendar_name}/{event_filename}"

        try:
            response = await self._client.delete(event_path)
            response.raise_for_status()

            logger.debug(f"Deleted event {event_uid}")
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"Event {event_uid} not found")
                return {"status_code": 404}
            logger.error(f"HTTP error deleting event: {e}")
            raise e
        except Exception as e:
            logger.error(f"Unexpected error deleting event: {e}")
            raise e

    async def get_event(
        self, calendar_name: str, event_uid: str
    ) -> Tuple[Dict[str, Any], str]:
        """Get detailed information about a specific event."""
        event_filename = f"{event_uid}.ics"
        event_path = f"{self._get_caldav_base_path()}/{calendar_name}/{event_filename}"

        headers = {"Accept": "text/calendar"}

        try:
            response = await self._client.get(event_path, headers=headers)
            response.raise_for_status()

            etag = response.headers.get("etag", "")
            event_data = self._parse_ical_event(response.text)

            if not event_data:
                raise ValueError(f"Failed to parse event data for {event_uid}")

            event_data["href"] = event_path
            event_data["etag"] = etag

            logger.debug(f"Retrieved event {event_uid}")
            return event_data, etag

        except HTTPStatusError as e:
            logger.error(f"HTTP error getting event: {e}")
            raise e
        except Exception as e:
            logger.error(f"Unexpected error getting event: {e}")
            raise e

    def _create_ical_event(self, event_data: Dict[str, Any], event_uid: str) -> str:
        """Create iCalendar content from event data."""
        cal = Calendar()
        cal.add("prodid", "-//NextCloud MCP Server//EN")
        cal.add("version", "2.0")

        event = ICalEvent()
        event.add("uid", event_uid)
        event.add("summary", event_data.get("title", ""))
        event.add("description", event_data.get("description", ""))
        event.add("location", event_data.get("location", ""))

        # Handle dates/times
        start_str = event_data.get("start_datetime", "")
        end_str = event_data.get("end_datetime", "")
        all_day = event_data.get("all_day", False)

        if start_str:  # Only parse if start_datetime is provided
            if all_day:
                start_date = datetime.fromisoformat(start_str.split("T")[0]).date()
                event.add("dtstart", start_date)
                if end_str:
                    end_date = datetime.fromisoformat(end_str.split("T")[0]).date()
                    event.add("dtend", end_date)
            else:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                event.add("dtstart", start_dt)
                if end_str:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    event.add("dtend", end_dt)

        # Add categories
        categories = event_data.get("categories", "")
        if categories:
            event.add("categories", categories.split(","))

        # Add priority and status
        priority = event_data.get("priority", 5)
        event.add("priority", priority)

        status = event_data.get("status", "CONFIRMED")
        event.add("status", status)

        # Add privacy classification
        privacy = event_data.get("privacy", "PUBLIC")
        event.add("class", privacy)

        # Add URL
        url = event_data.get("url", "")
        if url:
            event.add("url", url)

        # Handle recurrence
        recurring = event_data.get("recurring", False)
        if recurring:
            recurrence_rule = event_data.get("recurrence_rule", "")
            if recurrence_rule:
                event.add("rrule", vRecur.from_ical(recurrence_rule))

        # Add alarms/reminders
        reminder_minutes = event_data.get("reminder_minutes", 0)
        if reminder_minutes > 0:
            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", "Event reminder")
            alarm.add("trigger", timedelta(minutes=-reminder_minutes))
            event.add_component(alarm)

        # Add attendees
        attendees = event_data.get("attendees", "")
        if attendees:
            for email in attendees.split(","):
                if email.strip():
                    event.add("attendee", f"mailto:{email.strip()}")

        # Add timestamps
        now = datetime.utcnow()
        event.add("created", now)
        event.add("dtstamp", now)
        event.add("last-modified", now)

        cal.add_component(event)
        return cal.to_ical().decode("utf-8")

    def _parse_ical_event(self, ical_text: str) -> Optional[Dict[str, Any]]:
        """Parse iCalendar text and extract event data."""
        try:
            cal = Calendar.from_ical(ical_text)
            for component in cal.walk():
                if component.name == "VEVENT":
                    event_data = {
                        "uid": str(component.get("uid", "")),
                        "title": str(component.get("summary", "")),
                        "description": str(component.get("description", "")),
                        "location": str(component.get("location", "")),
                        "status": str(component.get("status", "CONFIRMED")),
                        "priority": int(component.get("priority", 5)),
                        "privacy": str(component.get("class", "PUBLIC")),
                        "url": str(component.get("url", "")),
                    }

                    # Handle dates
                    dtstart = component.get("dtstart")
                    if dtstart:
                        if isinstance(dtstart.dt, date) and not isinstance(
                            dtstart.dt, datetime
                        ):
                            event_data["start_datetime"] = dtstart.dt.isoformat()
                            event_data["all_day"] = True
                        else:
                            event_data["start_datetime"] = dtstart.dt.isoformat()
                            event_data["all_day"] = False

                    dtend = component.get("dtend")
                    if dtend:
                        if isinstance(dtend.dt, date) and not isinstance(
                            dtend.dt, datetime
                        ):
                            event_data["end_datetime"] = dtend.dt.isoformat()
                        else:
                            event_data["end_datetime"] = dtend.dt.isoformat()

                    # Handle categories
                    categories = component.get("categories")
                    if categories:
                        event_data["categories"] = self._extract_categories(categories)

                    # Handle recurrence
                    rrule = component.get("rrule")
                    if rrule:
                        event_data["recurring"] = True
                        event_data["recurrence_rule"] = str(rrule)

                    # Handle attendees
                    attendees = []
                    for attendee in component.get("attendee", []):
                        if isinstance(attendee, list):
                            attendees.extend(
                                str(a).replace("mailto:", "") for a in attendee
                            )
                        else:
                            attendees.append(str(attendee).replace("mailto:", ""))
                    if attendees:
                        event_data["attendees"] = ",".join(attendees)

                    return event_data

            return None

        except Exception as e:
            logger.error(f"Error parsing iCalendar: {e}")
            return None

    def _extract_categories(self, categories_obj) -> str:
        """Extract categories from icalendar object to string."""
        if not categories_obj:
            return ""

        try:
            # Handle icalendar vCategory objects
            if hasattr(categories_obj, "cats"):
                # vCategory object has a 'cats' attribute that's a list
                return ", ".join(str(cat) for cat in categories_obj.cats)
            elif hasattr(categories_obj, "__iter__") and not isinstance(
                categories_obj, str
            ):
                # Handle lists or other iterables
                return ", ".join(str(cat) for cat in categories_obj)
            else:
                # Handle strings or other objects
                return str(categories_obj)
        except Exception:
            # Fallback to string conversion
            return str(categories_obj)

    async def search_events_across_calendars(
        self,
        start_date: str = "",
        end_date: str = "",
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Search events across all calendars with advanced filtering."""
        try:
            calendars = await self.list_calendars()
            all_events = []

            for calendar in calendars:
                try:
                    events = await self.get_calendar_events(
                        calendar["name"], start_date, end_date
                    )

                    # Apply filters if provided
                    if filters:
                        events = self._apply_event_filters(events, filters)

                    # Add calendar info to each event
                    for event in events:
                        event["calendar_name"] = calendar["name"]
                        event["calendar_display_name"] = calendar.get(
                            "display_name", calendar["name"]
                        )

                    all_events.extend(events)
                except Exception as e:
                    logger.warning(
                        f"Error getting events from calendar {calendar['name']}: {e}"
                    )
                    continue

            return all_events

        except Exception as e:
            logger.error(f"Error searching events across calendars: {e}")
            raise

    def _apply_event_filters(
        self, events: List[Dict[str, Any]], filters: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Apply advanced filters to event list."""
        filtered_events = []

        for event in events:
            # Skip if event doesn't match filters
            if not self._event_matches_filters(event, filters):
                continue
            filtered_events.append(event)

        return filtered_events

    def _event_matches_filters(
        self, event: Dict[str, Any], filters: Dict[str, Any]
    ) -> bool:
        """Check if an event matches the provided filters."""
        try:
            # Filter by minimum attendees
            if "min_attendees" in filters:
                attendees = event.get("attendees", "")
                attendee_count = len(attendees.split(",")) if attendees else 0
                if attendee_count < filters["min_attendees"]:
                    return False

            # Filter by minimum duration
            if "min_duration_minutes" in filters:
                start_str = event.get("start_datetime", "")
                end_str = event.get("end_datetime", "")
                if start_str and end_str:
                    try:
                        start_dt = datetime.fromisoformat(
                            start_str.replace("Z", "+00:00")
                        )
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        duration_minutes = (end_dt - start_dt).total_seconds() / 60
                        if duration_minutes < filters["min_duration_minutes"]:
                            return False
                    except Exception:
                        pass

            # Filter by categories
            if "categories" in filters:
                event_categories = event.get("categories", "").lower()
                required_categories = [cat.lower() for cat in filters["categories"]]
                if not any(cat in event_categories for cat in required_categories):
                    return False

            # Filter by status
            if "status" in filters:
                if event.get("status", "").upper() != filters["status"].upper():
                    return False

            # Filter by title contains
            if "title_contains" in filters:
                title = event.get("title", "").lower()
                search_term = filters["title_contains"].lower()
                if search_term not in title:
                    return False

            # Filter by location contains
            if "location_contains" in filters:
                location = event.get("location", "").lower()
                search_term = filters["location_contains"].lower()
                if search_term not in location:
                    return False

            return True

        except Exception:
            # If filtering fails, include the event
            return True

    async def find_availability(
        self,
        duration_minutes: int,
        attendees: Optional[List[str]] = None,
        date_range_start: str = "",
        date_range_end: str = "",
        constraints: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Find available time slots for scheduling."""
        try:
            # Set default date range if not provided
            if not date_range_start:
                date_range_start = datetime.now().strftime("%Y-%m-%d")
            if not date_range_end:
                end_date = datetime.now() + timedelta(days=7)
                date_range_end = end_date.strftime("%Y-%m-%d")

            # Get all events in the date range
            busy_events = await self.search_events_across_calendars(
                start_date=date_range_start, end_date=date_range_end
            )

            # Filter events for relevant attendees if specified
            if attendees:
                relevant_events = []
                for event in busy_events:
                    event_attendees = event.get("attendees", "").lower()
                    if any(
                        attendee.lower() in event_attendees for attendee in attendees
                    ):
                        relevant_events.append(event)
                busy_events = relevant_events

            # Apply constraints
            constraints = constraints or {}
            business_hours_only = constraints.get("business_hours_only", False)
            exclude_weekends = constraints.get("exclude_weekends", False)
            preferred_times = constraints.get("preferred_times", [])

            # Generate time slots
            available_slots = self._generate_available_slots(
                busy_events,
                duration_minutes,
                date_range_start,
                date_range_end,
                business_hours_only,
                exclude_weekends,
                preferred_times,
            )

            return available_slots

        except Exception as e:
            logger.error(f"Error finding availability: {e}")
            raise

    def _generate_available_slots(
        self,
        busy_events: List[Dict[str, Any]],
        duration_minutes: int,
        start_date: str,
        end_date: str,
        business_hours_only: bool,
        exclude_weekends: bool,
        preferred_times: List[str],
    ) -> List[Dict[str, Any]]:
        """Generate available time slots."""
        available_slots = []

        try:
            current_date = datetime.fromisoformat(start_date)
            end_date_dt = datetime.fromisoformat(end_date)

            while current_date <= end_date_dt:
                # Skip weekends if requested
                if exclude_weekends and current_date.weekday() >= 5:
                    current_date += timedelta(days=1)
                    continue

                # Generate slots for this day
                day_slots = self._generate_day_slots(
                    current_date,
                    busy_events,
                    duration_minutes,
                    business_hours_only,
                    preferred_times,
                )
                available_slots.extend(day_slots)

                current_date += timedelta(days=1)

            return available_slots[:10]  # Limit to 10 slots

        except Exception as e:
            logger.error(f"Error generating available slots: {e}")
            return []

    def _generate_day_slots(
        self,
        date: datetime,
        busy_events: List[Dict[str, Any]],
        duration_minutes: int,
        business_hours_only: bool,
        preferred_times: List[str],
    ) -> List[Dict[str, Any]]:
        """Generate available slots for a specific day."""
        slots = []

        try:
            # Define working hours
            if business_hours_only:
                start_hour, end_hour = 9, 17
            else:
                start_hour, end_hour = 8, 20

            # Get busy periods for this day
            day_busy_periods = []
            for event in busy_events:
                try:
                    event_start = datetime.fromisoformat(
                        event["start_datetime"].replace("Z", "+00:00")
                    )
                    event_end = datetime.fromisoformat(
                        event["end_datetime"].replace("Z", "+00:00")
                    )

                    # Check if event is on this day
                    if event_start.date() == date.date():
                        day_busy_periods.append((event_start.time(), event_end.time()))
                except Exception:
                    continue

            # Sort busy periods
            day_busy_periods.sort()

            # Generate potential slots
            current_time = date.replace(
                hour=start_hour, minute=0, second=0, microsecond=0
            )
            end_time = date.replace(hour=end_hour, minute=0, second=0, microsecond=0)
            slot_duration = timedelta(minutes=duration_minutes)

            while current_time + slot_duration <= end_time:
                slot_end = current_time + slot_duration

                # Check if slot conflicts with any busy period
                if not self._slot_conflicts(
                    current_time.time(), slot_end.time(), day_busy_periods
                ):
                    # Check preferred times if specified
                    if not preferred_times or self._slot_in_preferred_times(
                        current_time.time(), preferred_times
                    ):
                        slots.append(
                            {
                                "start_datetime": current_time.isoformat(),
                                "end_datetime": slot_end.isoformat(),
                                "duration_minutes": duration_minutes,
                                "date": date.date().isoformat(),
                            }
                        )

                current_time += timedelta(minutes=30)  # 30-minute increments

            return slots

        except Exception as e:
            logger.error(f"Error generating day slots: {e}")
            return []

    def _slot_conflicts(self, slot_start, slot_end, busy_periods):
        """Check if a time slot conflicts with busy periods."""
        for busy_start, busy_end in busy_periods:
            if slot_start < busy_end and slot_end > busy_start:
                return True
        return False

    def _slot_in_preferred_times(self, slot_start, preferred_times):
        """Check if slot falls within preferred time ranges."""
        if not preferred_times:
            return True

        for time_range in preferred_times:
            try:
                start_str, end_str = time_range.split("-")
                pref_start = datetime.strptime(start_str, "%H:%M").time()
                pref_end = datetime.strptime(end_str, "%H:%M").time()

                if pref_start <= slot_start <= pref_end:
                    return True
            except Exception:
                continue

        return False

    async def bulk_update_events(
        self, filter_criteria: Dict[str, Any], update_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Bulk update events matching filter criteria."""
        try:
            # Find events matching criteria
            events = await self.search_events_across_calendars(
                start_date=filter_criteria.get("start_date", ""),
                end_date=filter_criteria.get("end_date", ""),
                filters=filter_criteria,
            )

            updated_count = 0
            failed_count = 0
            results = []

            for event in events:
                try:
                    # Update the event
                    await self.update_event(
                        event["calendar_name"], event["uid"], update_data
                    )
                    updated_count += 1
                    results.append(
                        {
                            "uid": event["uid"],
                            "status": "updated",
                            "title": event.get("title", ""),
                        }
                    )
                except Exception as e:
                    failed_count += 1
                    results.append(
                        {
                            "uid": event["uid"],
                            "status": "failed",
                            "error": str(e),
                            "title": event.get("title", ""),
                        }
                    )

            return {
                "total_found": len(events),
                "updated_count": updated_count,
                "failed_count": failed_count,
                "results": results,
            }

        except Exception as e:
            logger.error(f"Error in bulk update: {e}")
            raise

    async def create_calendar(
        self,
        calendar_name: str,
        display_name: str = "",
        description: str = "",
        color: str = "#1976D2",
    ) -> Dict[str, Any]:
        """Create a new calendar."""
        try:
            # Calendar creation via CalDAV MKCALENDAR
            calendar_path = f"{self._get_caldav_base_path()}/{calendar_name}/"

            # Create MKCALENDAR body
            mkcol_body = f"""<?xml version="1.0" encoding="utf-8"?>
            <mkcalendar xmlns="urn:ietf:params:xml:ns:caldav" xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">
                <d:set>
                    <d:prop>
                        <d:displayname>{display_name or calendar_name}</d:displayname>
                        <cs:calendar-color>{color}</cs:calendar-color>
                        <caldav:calendar-description xmlns:caldav="urn:ietf:params:xml:ns:caldav">{description}</caldav:calendar-description>
                        <caldav:supported-calendar-component-set xmlns:caldav="urn:ietf:params:xml:ns:caldav">
                            <caldav:comp name="VEVENT"/>
                        </caldav:supported-calendar-component-set>
                    </d:prop>
                </d:set>
            </mkcalendar>"""

            headers = {"Content-Type": "application/xml", "Depth": "0"}

            response = await self._client.request(
                "MKCALENDAR", calendar_path, content=mkcol_body, headers=headers
            )
            response.raise_for_status()

            logger.debug(f"Created calendar: {calendar_name}")
            return {
                "name": calendar_name,
                "display_name": display_name or calendar_name,
                "description": description,
                "color": color,
                "status_code": response.status_code,
            }

        except Exception as e:
            logger.error(f"Error creating calendar {calendar_name}: {e}")
            raise

    async def delete_calendar(self, calendar_name: str) -> Dict[str, Any]:
        """Delete a calendar."""
        try:
            calendar_path = f"{self._get_caldav_base_path()}/{calendar_name}/"

            response = await self._client.delete(calendar_path)
            response.raise_for_status()

            logger.debug(f"Deleted calendar: {calendar_name}")
            return {"status_code": response.status_code}

        except Exception as e:
            logger.error(f"Error deleting calendar {calendar_name}: {e}")
            raise
