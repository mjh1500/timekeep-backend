from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import asyncio
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional
import uuid
from datetime import datetime, time
from bson import ObjectId
import resend

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Resend email configuration
resend.api_key = os.environ.get('RESEND_API_KEY', '')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'onboarding@resend.dev')

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Helper function to convert ObjectId to string
def serialize_doc(doc):
    if doc:
        doc['_id'] = str(doc['_id'])
    return doc

# ===================
# Models
# ===================

class CrewMemberCreate(BaseModel):
    name: str
    auto_clockout_time: Optional[str] = "17:30"  # Default 5:30 PM in HH:MM format
    hourly_wage: Optional[float] = 0.0  # Hourly wage in dollars

class CrewMemberUpdate(BaseModel):
    name: Optional[str] = None
    auto_clockout_time: Optional[str] = None
    hourly_wage: Optional[float] = None

class CrewMember(BaseModel):
    id: str = Field(alias='_id')
    name: str
    auto_clockout_time: str = "17:30"  # Default 5:30 PM EST
    hourly_wage: float = 0.0  # Hourly wage in dollars
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True

class JobsiteCreate(BaseModel):
    name: str

class Jobsite(BaseModel):
    id: str = Field(alias='_id')
    name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True

# Project Segment Options
PROJECT_SEGMENTS = [
    "Not Applicable",
    "Siding",
    "Windows",
    "Doors",
    "Roofing",
    "Decks",
    "Drywall",
    "Paint",
    "Flooring",
    "Other"
]

class TimeEntryCreate(BaseModel):
    crew_member_id: str
    jobsite_id: str
    project_segment: Optional[str] = "Not Applicable"
    other_description: Optional[str] = None  # For "Other" category, max 150 chars

class TimeEntryUpdate(BaseModel):
    end_time: Optional[datetime] = None
    project_segment: Optional[str] = None
    other_description: Optional[str] = None

class TimeEntry(BaseModel):
    id: str = Field(alias='_id')
    crew_member_id: str
    crew_member_name: Optional[str] = None
    jobsite_id: str
    jobsite_name: Optional[str] = None
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_hours: Optional[float] = None
    is_active: bool = True
    auto_clocked_out: bool = False  # Flag for auto clock-out
    project_segment: str = "Not Applicable"
    other_description: Optional[str] = None
    
    class Config:
        populate_by_name = True

class ManHoursSummary(BaseModel):
    jobsite_id: str
    jobsite_name: str
    total_hours: float
    active_entries: int
    crew_members: List[str]

# Email Models
class EmailRequest(BaseModel):
    recipient_email: EmailStr
    
class DailyReportEmail(BaseModel):
    recipient_email: EmailStr
    date: Optional[str] = None  # YYYY-MM-DD format, defaults to today

# ===================
# Project Segments Endpoint
# ===================

@api_router.get("/project-segments")
async def get_project_segments():
    """Get available project segment options"""
    return {"segments": PROJECT_SEGMENTS}

# ===================
# Crew Member Endpoints
# ===================

@api_router.get("/")
async def root():
    return {"message": "TimeKeep API"}

@api_router.post("/crew-members", response_model=CrewMember)
async def create_crew_member(input: CrewMemberCreate):
    doc = {
        "name": input.name,
        "auto_clockout_time": input.auto_clockout_time or "17:30",
        "hourly_wage": input.hourly_wage or 0.0,
        "created_at": datetime.utcnow()
    }
    result = await db.crew_members.insert_one(doc)
    doc['_id'] = str(result.inserted_id)
    return CrewMember(**doc)

@api_router.get("/crew-members", response_model=List[CrewMember])
async def get_crew_members():
    members = await db.crew_members.find().sort("name", 1).to_list(1000)
    # Add defaults for existing members
    for m in members:
        if 'auto_clockout_time' not in m:
            m['auto_clockout_time'] = "17:30"
        if 'hourly_wage' not in m:
            m['hourly_wage'] = 0.0
    return [CrewMember(**serialize_doc(m)) for m in members]

@api_router.put("/crew-members/{member_id}", response_model=CrewMember)
async def update_crew_member(member_id: str, input: CrewMemberUpdate):
    update_data = {}
    if input.name is not None:
        update_data["name"] = input.name
    if input.auto_clockout_time is not None:
        update_data["auto_clockout_time"] = input.auto_clockout_time
    if input.hourly_wage is not None:
        update_data["hourly_wage"] = input.hourly_wage
    
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    result = await db.crew_members.update_one(
        {"_id": ObjectId(member_id)},
        {"$set": update_data}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Crew member not found")
    
    updated = await db.crew_members.find_one({"_id": ObjectId(member_id)})
    if 'auto_clockout_time' not in updated:
        updated['auto_clockout_time'] = "17:30"
    if 'hourly_wage' not in updated:
        updated['hourly_wage'] = 0.0
    return CrewMember(**serialize_doc(updated))

@api_router.delete("/crew-members/{member_id}")
async def delete_crew_member(member_id: str):
    result = await db.crew_members.delete_one({"_id": ObjectId(member_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Crew member not found")
    return {"message": "Crew member deleted"}

# ===================
# Jobsite Endpoints
# ===================

@api_router.post("/jobsites", response_model=Jobsite)
async def create_jobsite(input: JobsiteCreate):
    # Check if jobsite already exists (case insensitive)
    existing = await db.jobsites.find_one({"name": {"$regex": f"^{input.name}$", "$options": "i"}})
    if existing:
        return Jobsite(**serialize_doc(existing))
    
    doc = {
        "name": input.name,
        "created_at": datetime.utcnow()
    }
    result = await db.jobsites.insert_one(doc)
    doc['_id'] = str(result.inserted_id)
    return Jobsite(**doc)

@api_router.get("/jobsites", response_model=List[Jobsite])
async def get_jobsites():
    jobsites = await db.jobsites.find().sort("name", 1).to_list(1000)
    return [Jobsite(**serialize_doc(j)) for j in jobsites]

@api_router.get("/jobsites/search")
async def search_jobsites(q: str = ""):
    """Search jobsites for autocomplete"""
    if not q:
        jobsites = await db.jobsites.find().sort("created_at", -1).limit(10).to_list(10)
    else:
        jobsites = await db.jobsites.find(
            {"name": {"$regex": q, "$options": "i"}}
        ).sort("name", 1).limit(10).to_list(10)
    return [Jobsite(**serialize_doc(j)) for j in jobsites]

@api_router.delete("/jobsites/{jobsite_id}")
async def delete_jobsite(jobsite_id: str):
    result = await db.jobsites.delete_one({"_id": ObjectId(jobsite_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Jobsite not found")
    return {"message": "Jobsite deleted"}

# ===================
# Time Entry Endpoints
# ===================

@api_router.post("/time-entries", response_model=TimeEntry)
async def start_time_entry(input: TimeEntryCreate):
    """Start a new time entry (clock in)"""
    # Check if there's already an active entry for this crew member
    existing = await db.time_entries.find_one({
        "crew_member_id": input.crew_member_id,
        "is_active": True
    })
    if existing:
        raise HTTPException(status_code=400, detail="Crew member already has an active time entry")
    
    # Get crew member name
    crew_member = await db.crew_members.find_one({"_id": ObjectId(input.crew_member_id)})
    if not crew_member:
        raise HTTPException(status_code=404, detail="Crew member not found")
    
    # Get or create jobsite
    jobsite = await db.jobsites.find_one({"_id": ObjectId(input.jobsite_id)})
    if not jobsite:
        raise HTTPException(status_code=404, detail="Jobsite not found")
    
    # Validate project segment
    project_segment = input.project_segment or "Not Applicable"
    if project_segment not in PROJECT_SEGMENTS:
        project_segment = "Not Applicable"
    
    # Validate other_description (max 150 chars)
    other_description = None
    if project_segment == "Other" and input.other_description:
        other_description = input.other_description[:150]
    
    doc = {
        "crew_member_id": input.crew_member_id,
        "crew_member_name": crew_member["name"],
        "jobsite_id": input.jobsite_id,
        "jobsite_name": jobsite["name"],
        "start_time": datetime.utcnow(),
        "end_time": None,
        "duration_hours": None,
        "is_active": True,
        "project_segment": project_segment,
        "other_description": other_description
    }
    result = await db.time_entries.insert_one(doc)
    doc['_id'] = str(result.inserted_id)
    return TimeEntry(**doc)

@api_router.put("/time-entries/{entry_id}/stop", response_model=TimeEntry)
async def stop_time_entry(entry_id: str):
    """Stop a time entry (clock out)"""
    entry = await db.time_entries.find_one({"_id": ObjectId(entry_id)})
    if not entry:
        raise HTTPException(status_code=404, detail="Time entry not found")
    
    if not entry.get("is_active"):
        raise HTTPException(status_code=400, detail="Time entry already stopped")
    
    end_time = datetime.utcnow()
    start_time = entry["start_time"]
    duration = (end_time - start_time).total_seconds() / 3600  # Convert to hours
    
    await db.time_entries.update_one(
        {"_id": ObjectId(entry_id)},
        {
            "$set": {
                "end_time": end_time,
                "duration_hours": round(duration, 2),
                "is_active": False
            }
        }
    )
    
    updated = await db.time_entries.find_one({"_id": ObjectId(entry_id)})
    return TimeEntry(**serialize_doc(updated))

@api_router.get("/time-entries", response_model=List[TimeEntry])
async def get_time_entries(active_only: bool = False, jobsite_id: Optional[str] = None):
    """Get all time entries with optional filters"""
    query = {}
    if active_only:
        query["is_active"] = True
    if jobsite_id:
        query["jobsite_id"] = jobsite_id
    
    entries = await db.time_entries.find(query).sort("start_time", -1).to_list(1000)
    return [TimeEntry(**serialize_doc(e)) for e in entries]

@api_router.get("/time-entries/active", response_model=List[TimeEntry])
async def get_active_entries():
    """Get all currently active time entries"""
    entries = await db.time_entries.find({"is_active": True}).to_list(1000)
    return [TimeEntry(**serialize_doc(e)) for e in entries]

@api_router.delete("/time-entries/{entry_id}")
async def delete_time_entry(entry_id: str):
    result = await db.time_entries.delete_one({"_id": ObjectId(entry_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Time entry not found")
    return {"message": "Time entry deleted"}

# ===================
# Dashboard / Summary Endpoints
# ===================

@api_router.get("/dashboard/summary", response_model=List[ManHoursSummary])
async def get_dashboard_summary():
    """Get man hours summary for all jobsites"""
    jobsites = await db.jobsites.find().to_list(1000)
    summaries = []
    
    for jobsite in jobsites:
        jobsite_id = str(jobsite["_id"])
        
        # Get all time entries for this jobsite
        entries = await db.time_entries.find({"jobsite_id": jobsite_id}).to_list(1000)
        
        total_hours = 0
        active_entries = 0
        crew_members = set()
        
        for entry in entries:
            if entry.get("duration_hours"):
                total_hours += entry["duration_hours"]
            if entry.get("is_active"):
                active_entries += 1
                # Calculate running hours for active entries
                running_hours = (datetime.utcnow() - entry["start_time"]).total_seconds() / 3600
                total_hours += running_hours
            if entry.get("crew_member_name"):
                crew_members.add(entry["crew_member_name"])
        
        summaries.append(ManHoursSummary(
            jobsite_id=jobsite_id,
            jobsite_name=jobsite["name"],
            total_hours=round(total_hours, 2),
            active_entries=active_entries,
            crew_members=list(crew_members)
        ))
    
    return summaries

@api_router.get("/dashboard/active-crews")
async def get_active_crews():
    """Get currently active crews grouped by jobsite"""
    entries = await db.time_entries.find({"is_active": True}).to_list(1000)
    
    grouped = {}
    for entry in entries:
        jobsite_name = entry.get("jobsite_name", "Unknown")
        if jobsite_name not in grouped:
            grouped[jobsite_name] = {
                "jobsite_id": entry["jobsite_id"],
                "jobsite_name": jobsite_name,
                "crew_members": []
            }
        grouped[jobsite_name]["crew_members"].append({
            "entry_id": str(entry["_id"]),
            "crew_member_id": entry["crew_member_id"],
            "crew_member_name": entry.get("crew_member_name", "Unknown"),
            "start_time": entry["start_time"].isoformat()
        })
    
    return list(grouped.values())

# ===================
# Email Endpoints
# ===================

@api_router.post("/send-daily-report")
async def send_daily_report(request: DailyReportEmail):
    """Send daily time tracking report with payroll via email"""
    try:
        # Get today's date or specified date
        if request.date:
            report_date = datetime.strptime(request.date, "%Y-%m-%d")
        else:
            report_date = datetime.utcnow()
        
        start_of_day = report_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = report_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        # Get all time entries for the day
        entries = await db.time_entries.find({
            "start_time": {"$gte": start_of_day, "$lte": end_of_day}
        }).to_list(1000)
        
        # Get all crew members for wage lookup
        crew_members = await db.crew_members.find().to_list(1000)
        crew_wages = {str(c["_id"]): c.get("hourly_wage", 0) for c in crew_members}
        
        # Build report data by jobsite
        jobsite_data = {}
        total_hours = 0
        total_payroll = 0
        
        for entry in entries:
            jobsite_name = entry.get("jobsite_name", "Unknown")
            if jobsite_name not in jobsite_data:
                jobsite_data[jobsite_name] = {
                    "entries": [],
                    "total_hours": 0,
                    "total_payroll": 0
                }
            
            hours = entry.get("duration_hours", 0) or 0
            if entry.get("is_active"):
                # Calculate running hours for active entries
                hours = (datetime.utcnow() - entry["start_time"]).total_seconds() / 3600
            
            # Get wage for this crew member
            crew_member_id = entry.get("crew_member_id", "")
            hourly_wage = crew_wages.get(crew_member_id, 0)
            pay = round(hours * hourly_wage, 2)
            
            # Get project segment info
            project_segment = entry.get("project_segment", "Not Applicable")
            other_description = entry.get("other_description", "")
            segment_display = project_segment
            if project_segment == "Other" and other_description:
                segment_display = f"Other: {other_description}"
            
            jobsite_data[jobsite_name]["entries"].append({
                "crew_member": entry.get("crew_member_name", "Unknown"),
                "crew_member_id": crew_member_id,
                "hours": round(hours, 2),
                "hourly_wage": hourly_wage,
                "pay": pay,
                "is_active": entry.get("is_active", False),
                "auto_clocked_out": entry.get("auto_clocked_out", False),
                "project_segment": segment_display
            })
            jobsite_data[jobsite_name]["total_hours"] += hours
            jobsite_data[jobsite_name]["total_payroll"] += pay
            total_hours += hours
            total_payroll += pay
        
        # Build HTML email
        date_str = report_date.strftime("%B %d, %Y")
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background-color: #f5f5f5; padding: 20px;">
            <div style="max-width: 700px; margin: 0 auto; background-color: white; border-radius: 10px; padding: 20px;">
                <h1 style="color: #1a1a2e; border-bottom: 2px solid #4ade80; padding-bottom: 10px;">
                    Daily Time & Payroll Report - {date_str}
                </h1>
                
                <div style="display: flex; gap: 10px; margin: 20px 0;">
                    <div style="flex: 1; background-color: #166534; color: white; padding: 15px; border-radius: 8px; text-align: center;">
                        <h3 style="margin: 0; font-size: 14px;">Total Man Hours</h3>
                        <p style="margin: 5px 0 0 0; font-size: 28px; font-weight: bold;">{round(total_hours, 2)}</p>
                    </div>
                    <div style="flex: 1; background-color: #1d4ed8; color: white; padding: 15px; border-radius: 8px; text-align: center;">
                        <h3 style="margin: 0; font-size: 14px;">Total Payroll</h3>
                        <p style="margin: 5px 0 0 0; font-size: 28px; font-weight: bold;">${round(total_payroll, 2):,.2f}</p>
                    </div>
                </div>
        """
        
        for jobsite, data in jobsite_data.items():
            html_content += f"""
                <div style="background-color: #f8f9fa; padding: 15px; border-radius: 8px; margin: 15px 0; border-left: 4px solid #60a5fa;">
                    <h3 style="color: #1a1a2e; margin-top: 0;">{jobsite}</h3>
                    <p style="margin: 5px 0;"><strong style="color: #4ade80;">Hours:</strong> {round(data['total_hours'], 2)} | <strong style="color: #3b82f6;">Payroll:</strong> ${round(data['total_payroll'], 2):,.2f}</p>
                    <table style="width: 100%; border-collapse: collapse; margin-top: 10px;">
                        <tr style="background-color: #e5e7eb;">
                            <th style="padding: 8px; text-align: left;">Crew Member</th>
                            <th style="padding: 8px; text-align: left;">Project Segment</th>
                            <th style="padding: 8px; text-align: right;">Hours</th>
                            <th style="padding: 8px; text-align: right;">Rate</th>
                            <th style="padding: 8px; text-align: right;">Pay</th>
                            <th style="padding: 8px; text-align: center;">Status</th>
                        </tr>
            """
            for entry in data["entries"]:
                status = ""
                if entry["is_active"]:
                    status = '<span style="color: #16a34a;">Active</span>'
                elif entry["auto_clocked_out"]:
                    status = '<span style="color: #f59e0b;">Auto</span>'
                else:
                    status = '<span style="color: #6b7280;">Done</span>'
                
                wage_display = f"${entry['hourly_wage']:.2f}/hr" if entry['hourly_wage'] > 0 else "Not set"
                segment_display = entry.get('project_segment', 'Not Applicable')
                
                html_content += f"""
                        <tr>
                            <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{entry['crew_member']}</td>
                            <td style="padding: 8px; border-bottom: 1px solid #e5e7eb; color: #7c3aed; font-weight: 500;">{segment_display}</td>
                            <td style="padding: 8px; border-bottom: 1px solid #e5e7eb; text-align: right;">{entry['hours']}</td>
                            <td style="padding: 8px; border-bottom: 1px solid #e5e7eb; text-align: right;">{wage_display}</td>
                            <td style="padding: 8px; border-bottom: 1px solid #e5e7eb; text-align: right; font-weight: bold;">${entry['pay']:.2f}</td>
                            <td style="padding: 8px; border-bottom: 1px solid #e5e7eb; text-align: center;">{status}</td>
                        </tr>
                """
            html_content += """
                    </table>
                </div>
            """
        
        if not jobsite_data:
            html_content += """
                <p style="color: #6b7280; text-align: center; padding: 20px;">No time entries recorded for this day.</p>
            """
        
        html_content += """
            </div>
        </body>
        </html>
        """
        
        # Send email using Resend
        params = {
            "from": SENDER_EMAIL,
            "to": [request.recipient_email],
            "subject": f"Daily Time & Payroll Report - {date_str}",
            "html": html_content
        }
        
        email = await asyncio.to_thread(resend.Emails.send, params)
        
        return {
            "status": "success",
            "message": f"Daily report sent to {request.recipient_email}",
            "email_id": email.get("id"),
            "total_hours": round(total_hours, 2),
            "total_payroll": round(total_payroll, 2),
            "jobsites_count": len(jobsite_data)
        }
        
    except Exception as e:
        logger.error(f"Failed to send email: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to send email: {str(e)}")

# ===================
# Auto Clock-Out Endpoints  
# ===================

@api_router.post("/auto-clockout/check")
async def check_auto_clockout():
    """
    Check and process auto clock-outs for crew members past their scheduled time.
    This should be called periodically (e.g., every minute) from the frontend.
    Returns list of crew members that were auto-clocked out.
    """
    from datetime import timezone
    import pytz
    
    # Get EST timezone
    est = pytz.timezone('America/New_York')
    now_est = datetime.now(est)
    current_time_str = now_est.strftime("%H:%M")
    
    auto_clocked = []
    
    # Get all active time entries
    active_entries = await db.time_entries.find({"is_active": True}).to_list(1000)
    
    for entry in active_entries:
        crew_member_id = entry["crew_member_id"]
        
        # Get crew member's auto clock-out time
        crew_member = await db.crew_members.find_one({"_id": ObjectId(crew_member_id)})
        if not crew_member:
            continue
        
        auto_clockout_time = crew_member.get("auto_clockout_time", "17:30")
        
        # Check if current time is past the auto clock-out time
        if current_time_str >= auto_clockout_time:
            # Perform auto clock-out
            end_time = datetime.utcnow()
            start_time = entry["start_time"]
            duration = (end_time - start_time).total_seconds() / 3600
            
            await db.time_entries.update_one(
                {"_id": entry["_id"]},
                {
                    "$set": {
                        "end_time": end_time,
                        "duration_hours": round(duration, 2),
                        "is_active": False,
                        "auto_clocked_out": True
                    }
                }
            )
            
            auto_clocked.append({
                "entry_id": str(entry["_id"]),
                "crew_member_id": crew_member_id,
                "crew_member_name": entry.get("crew_member_name", "Unknown"),
                "jobsite_name": entry.get("jobsite_name", "Unknown"),
                "duration_hours": round(duration, 2),
                "auto_clockout_time": auto_clockout_time
            })
    
    return {
        "checked_at": now_est.isoformat(),
        "current_time_est": current_time_str,
        "auto_clocked_count": len(auto_clocked),
        "auto_clocked": auto_clocked
    }

@api_router.get("/crew-members/{member_id}/clockout-time")
async def get_crew_clockout_time(member_id: str):
    """Get a crew member's auto clock-out time"""
    member = await db.crew_members.find_one({"_id": ObjectId(member_id)})
    if not member:
        raise HTTPException(status_code=404, detail="Crew member not found")
    
    return {
        "crew_member_id": member_id,
        "name": member["name"],
        "auto_clockout_time": member.get("auto_clockout_time", "17:30")
    }

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
