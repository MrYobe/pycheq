from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    
    def set_password(self, password):
        """Set password hash for the user"""
        self.password = generate_password_hash(password)
        
    def check_password(self, password):
        """Check if the provided password matches the hash"""
        return check_password_hash(self.password, password)

class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_code = db.Column(db.String(10), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    nrc = db.Column(db.String(20), unique=True, nullable=True)
    email = db.Column(db.String(100), unique=True, nullable=True)
    position = db.Column(db.String(100), nullable=False)
    department = db.Column(db.String(100), nullable=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    branch = db.relationship('Branch', backref=db.backref('employees', lazy=True))
    basic_salary = db.Column(db.Float, nullable=False)
    housing_allowance = db.Column(db.Float, default=0)
    lunch_allowance = db.Column(db.Float, default=0)
    transport_allowance = db.Column(db.Float, default=0)
    overtime = db.Column(db.Float, default=0)
    salary_advance = db.Column(db.Float, default=0)
    rainbow_loan = db.Column(db.Float, default=0)
    other_deduction = db.Column(db.Float, default=0)
    other_deduction_reason = db.Column(db.String(200), nullable=True)
    hire_date = db.Column(db.DateTime, default=datetime.now)
    is_active = db.Column(db.Boolean, default=True)
    payroll_records = db.relationship('PayrollRecord', backref='employee', lazy=True, cascade='all, delete-orphan', overlaps="employee_rel,payroll_records_list")

    @property
    def total_salary(self):
        return self.basic_salary + self.housing_allowance + self.lunch_allowance + self.transport_allowance + self.overtime

    def calculate_paye(self):
        """Calculate PAYE tax based on the current tax brackets."""
        tax_settings = TaxSettings.query.first()
        
        # If no tax settings found, use default rates
        if not tax_settings:
            tax_settings = TaxSettings(
                bracket1=0,    # 0% up to K5,100
                bracket2=20,   # 20% K5,100.01 - K7,100
                bracket3=30,   # 30% K7,100.01 - K9,200
                bracket4=37    # 37% above K9,200
            )
        
        paye = 0
        gross_pay = self.total_salary
        
        # No tax on first K5,100
        remaining_pay = max(0, gross_pay - 5100)
        
        # 20% on income between K5,100.01 and K7,100 (K2,000 band)
        if remaining_pay > 0:
            taxable = min(remaining_pay, 2000)
            paye += taxable * (tax_settings.bracket2 / 100)
            remaining_pay -= taxable
        
        # 30% on income between K7,100.01 and K9,200 (K2,100 band)
        if remaining_pay > 0:
            taxable = min(remaining_pay, 2100)
            paye += taxable * (tax_settings.bracket3 / 100)
            remaining_pay -= taxable
        
        # 37% on income above K9,200
        if remaining_pay > 0:
            paye += remaining_pay * (tax_settings.bracket4 / 100)
        
        return paye

    def calculate_napsa(self):
        napsa_ceiling = 1221.80
        contribution = self.total_salary * 0.05
        return min(contribution, napsa_ceiling)

    def calculate_nhima(self):
        return self.basic_salary * 0.01

    def calculate_total_deductions(self):
        return (self.calculate_paye() +
                self.calculate_napsa() +
                self.calculate_nhima() +
                self.salary_advance +
                self.rainbow_loan +
                self.other_deduction)

    def calculate_net_pay(self):
        return self.total_salary - self.calculate_total_deductions()

class License(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    license_key = db.Column(db.String(100), unique=True, nullable=False)
    start_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    end_date = db.Column(db.DateTime, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def is_valid(self):
        return self.is_active and self.end_date > datetime.utcnow()

class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.Text, nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    website = db.Column(db.String(120))
    registration_number = db.Column(db.String(50), nullable=False)
    tax_number = db.Column(db.String(50), nullable=False)
    logo = db.Column(db.String(255))
    email_signature = db.Column(db.Text)
    email_footer = db.Column(db.Text)
    licenses = db.relationship('License', backref='company', lazy=True)

    @property
    def current_license(self):
        return License.query.filter_by(company_id=self.id, is_active=True).order_by(License.end_date.desc()).first()

    @property
    def is_licensed(self):
        current_license = self.current_license
        return current_license and current_license.is_valid

class Branch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    address = db.Column(db.Text, nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    manager = db.Column(db.String(100), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

class PayrollRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
    pay_date = db.Column(db.String(10), nullable=False, default=lambda: datetime.now().strftime('%Y-%m-%d'))
    basic_salary = db.Column(db.Float, nullable=False)
    housing_allowance = db.Column(db.Float, nullable=False)
    lunch_allowance = db.Column(db.Float, nullable=False)
    transport_allowance = db.Column(db.Float, nullable=False)
    overtime = db.Column(db.Float, nullable=False)
    gross_pay = db.Column(db.Float, nullable=False)
    paye = db.Column(db.Float, nullable=False)
    napsa = db.Column(db.Float, nullable=False)
    nhima = db.Column(db.Float, nullable=False)
    salary_advance = db.Column(db.Float, default=0.0)
    rainbow_loan = db.Column(db.Float, default=0.0)
    other_deduction = db.Column(db.Float, default=0.0)
    other_deduction_reason = db.Column(db.String(200), nullable=True)
    total_deductions = db.Column(db.Float, nullable=False)
    net_pay = db.Column(db.Float, nullable=False)

class TaxSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bracket1 = db.Column(db.Float, nullable=False, default=0)
    bracket2 = db.Column(db.Float, nullable=False, default=20)
    bracket3 = db.Column(db.Float, nullable=False, default=30)
    bracket4 = db.Column(db.Float, nullable=False, default=37)

class EmailSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, default=False)
    smtp_server = db.Column(db.String(100), default='smtp.gmail.com')
    smtp_port = db.Column(db.Integer, default=587)
    smtp_use_tls = db.Column(db.Boolean, default=True)
    smtp_username = db.Column(db.String(100))
    smtp_password = db.Column(db.String(100))
    default_sender = db.Column(db.String(100))
    last_tested = db.Column(db.DateTime)
    test_status = db.Column(db.String(100))

class Report(db.Model):
    id = db.Column(db.String(100), primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    format = db.Column(db.String(10), nullable=False)
    generated_at = db.Column(db.DateTime, default=datetime.now)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=True)
    file_path = db.Column(db.String(255), nullable=False) 