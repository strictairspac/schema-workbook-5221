from sqlalchemy import Column,Integer,String,JSON,DateTime,ForeignKey,Text
from datetime import datetime,timezone
from src.config.database import Base

def utcnow(): return datetime.now(timezone.utc)

class User(Base):
    __tablename__='users'
    id=Column(Integer,primary_key=True);email=Column(String(120),unique=True,nullable=False);password=Column(String(300),nullable=False);role=Column(String(20),nullable=False)
class Relation(Base):
    __tablename__='relations'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False);title=Column(String(160),nullable=False);attributes=Column(JSON,nullable=False);note=Column(String(300),default='')
class Dependency(Base):
    __tablename__='dependencies'
    id=Column(Integer,primary_key=True);relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False);lhs=Column(JSON,nullable=False);rhs=Column(JSON,nullable=False);note=Column(String(300),default='')
class Report(Base):
    __tablename__='reports'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False);relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False)
    status=Column(String(20),nullable=False,default='queued');input_snapshot=Column(JSON,nullable=False);result=Column(JSON,nullable=True);error=Column(Text,nullable=True)
    created_at=Column(DateTime(timezone=True),default=utcnow)
class Drill(Base):
    """A frozen data-migration rehearsal for one report. The plan never reads live drafts after creation."""
    __tablename__='drills'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False)
    relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False);report_id=Column(Integer,ForeignKey('reports.id'),nullable=False)
    status=Column(String(20),nullable=False,default='pending');plan=Column(JSON,nullable=False)
    error=Column(Text,nullable=True);progress=Column(JSON,nullable=True)
    source_checksum=Column(String(64),nullable=True);new_checksum=Column(String(64),nullable=True);row_count=Column(Integer,nullable=True)
    created_at=Column(DateTime(timezone=True),default=utcnow);updated_at=Column(DateTime(timezone=True),default=utcnow,onupdate=utcnow)
    switched_at=Column(DateTime(timezone=True),nullable=True)
class DrillAssignment(Base):
    """One commission table may have exactly one decomposition plan take effect."""
    __tablename__='drill_assignments'
    relation_id=Column(Integer,primary_key=True);drill_id=Column(Integer,nullable=False,unique=True)
    created_at=Column(DateTime(timezone=True),default=utcnow)
class RelationLock(Base):
    """Serializes source-table writes against migration/switch for one commission table."""
    __tablename__='relation_locks'
    relation_id=Column(Integer,primary_key=True)
class DrillChange(Base):
    __tablename__='drill_changes'
    id=Column(Integer,primary_key=True);drill_id=Column(Integer,ForeignKey('drills.id'),nullable=False);row_key=Column(String(80),nullable=False)
    action=Column(String(20),nullable=False);before=Column(JSON,nullable=True);after=Column(JSON,nullable=True);created_at=Column(DateTime(timezone=True),default=utcnow)
class Audit(Base):
    __tablename__='audit_logs'
    id=Column(Integer,primary_key=True);actor=Column(String(120),nullable=False);action=Column(String(160),nullable=False);created_at=Column(DateTime(timezone=True),default=utcnow)
