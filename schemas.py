"""
Database Schemas for Messaging App

Each Pydantic model represents a collection in MongoDB.
Collection name is the lowercase of the class name.
"""
from pydantic import BaseModel, Field
from typing import Optional, List

class Customer(BaseModel):
    name: str = Field(..., description="Full name of customer")
    email: str = Field(..., description="Email address of customer")
    phone: Optional[str] = Field(None, description="Phone number")
    account_id: Optional[str] = Field(None, description="Internal account identifier")
    is_vip: bool = Field(False, description="Whether the customer is VIP/high priority")
    last_loan_status: Optional[str] = Field(None, description="Loan status such as pending, approved, disbursed")
    kyc_status: Optional[str] = Field(None, description="KYC status")
    notes: Optional[str] = Field(None, description="Additional internal notes about the customer")

class Message(BaseModel):
    customer_id: str = Field(..., description="Reference to the customer")
    text: str = Field(..., description="Message body text")
    channel: str = Field("web", description="Source channel such as web, sms, email")
    direction: str = Field("inbound", description="inbound for customer to agent, outbound for agent to customer")
    status: str = Field("open", description="open or closed")
    urgency_score: int = Field(0, ge=0, le=100, description="Computed urgency score 0-100")
    topic: Optional[str] = Field(None, description="Optional detected topic e.g., loan, account, kyc")

class Cannedmessage(BaseModel):
    title: str = Field(..., description="Short title for canned response")
    text: str = Field(..., description="Full canned response text")
    tags: List[str] = Field(default_factory=list, description="Tags for quick filtering")
