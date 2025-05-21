from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, session, abort, make_response, send_from_directory
from flask_migrate import Migrate
from sqlalchemy import extract, func, distinct, text
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from sqlalchemy import func, Date
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os
import calendar
from werkzeug.utils import secure_filename
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from io import BytesIO
import pandas as pd
import numpy as np
import json
import uuid
import shutil
from flask_mail import Mail, Message
import threading
import re
from fixed_email_utils import send_smtp_email
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email import encoders
from models import db, User, Employee, License, Company, Branch, PayrollRecord, TaxSettings, EmailSettings, Report
import ssl
import socket
import csv
import io

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///payroll.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Email configuration
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = None  # To be configured in settings
app.config['MAIL_PASSWORD'] = None  # To be configured in settings
app.config['MAIL_DEFAULT_SENDER'] = None  # To be configured in settings

mail = Mail(app)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize extensions
db.init_app(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Register blueprints
from license_routes import license_bp
app.register_blueprint(license_bp)

@app.template_filter('month_name')
def month_name(month_number):
    return calendar.month_name[month_number]

@app.template_filter('get_month_name')
def get_month_name(month_number):
    """Convert month number to month name"""
    month_names = ['January', 'February', 'March', 'April', 'May', 'June', 
                  'July', 'August', 'September', 'October', 'November', 'December']
    return month_names[month_number - 1] if 1 <= month_number <= 12 else ''

@app.template_filter('format_currency')
def format_currency(value):
    """Format a number as currency with commas and 2 decimal places"""
    return "{:,.2f}".format(value)

@app.template_filter('file_size')
def file_size_filter(file_path):
    try:
        file_path = os.path.join(app.root_path, file_path)
        if os.path.exists(file_path):
            return os.path.getsize(file_path)
        return 0
    except:
        return 0

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Email utility functions


def send_email(subject, recipients, html_body, attachments=None):
    """Send an email with optional attachments"""
    # Check if email is configured and enabled
    email_settings = EmailSettings.query.first()
    if not email_settings or not email_settings.enabled:
        return False, "Email not configured or disabled"
    
    try:
        # Get email settings
        server = email_settings.smtp_server
        port = email_settings.smtp_port
        use_tls = email_settings.smtp_use_tls
        username = email_settings.smtp_username
        password = email_settings.smtp_password
        sender = email_settings.default_sender or username
        
        # Validate settings
        if not server or not port:
            return False, "SMTP server or port not configured"
        
        # Create message
        msg = MIMEMultipart()
        msg['From'] = sender
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        
        # Attach HTML body
        msg.attach(MIMEText(html_body, 'html'))
        
        # Add attachments if any
        if attachments:
            for attachment in attachments:
                if isinstance(attachment, tuple) and len(attachment) == 2:
                    filename, content = attachment
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(content)
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
                    msg.attach(part)
        
        # Set up SSL context
        context = ssl.create_default_context()
        
        # Connect to SMTP server and send email
        try:
            if port == 465:  # SSL
                smtp = smtplib.SMTP_SSL(server, port, context=context, timeout=30)
            else:  # TLS or regular
                smtp = smtplib.SMTP(server, port, timeout=30)
                if use_tls:
                    smtp.starttls(context=context)
            
            # Login if credentials provided
            if username and password:
                smtp.login(username, password)
            
            # Send email
            smtp.send_message(msg)
            smtp.quit()
            return True, "Email sent successfully"
            
        except smtplib.SMTPConnectError as e:
            return False, f"Failed to connect to SMTP server: {str(e)}"
        except smtplib.SMTPAuthenticationError as e:
            return False, f"SMTP authentication failed: {str(e)}"
        except smtplib.SMTPException as e:
            return False, f"SMTP error occurred: {str(e)}"
        except socket.timeout:
            return False, "Connection to SMTP server timed out"
        except socket.error as e:
            return False, f"Network error: {str(e)}"
        
    except Exception as e:
        print(f"Error sending email: {str(e)}")
        return False, f"Failed to send email: {str(e)}"

def send_payslip_email(employee_id, payroll_record_id, custom_email=None):
    """Send a payslip to an employee via email"""
    employee = Employee.query.get(employee_id)
    payroll_record = PayrollRecord.query.get(payroll_record_id)
    company = Company.query.first()
    
    if not employee or not payroll_record or not company:
        return False, "Missing employee, payroll record, or company information"
    
    if not custom_email and not employee.email:
        return False, f"Employee {employee.name} does not have an email address"
    
    # Generate payslip PDF
    # Convert string date to datetime object
    if isinstance(payroll_record.pay_date, str):
        pay_date = datetime.strptime(payroll_record.pay_date, '%Y-%m-%d')
        month_name = calendar.month_name[pay_date.month]
    else:
        month_name = calendar.month_name[payroll_record.pay_date.month]
    
    pdf_buffer = BytesIO()
    
    # Create the PDF document
    doc = SimpleDocTemplate(pdf_buffer, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Center', alignment=1))
    styles.add(ParagraphStyle(name='Right', alignment=2))
    styles.add(ParagraphStyle(name='PayslipTitle', fontName='Helvetica-Bold', fontSize=16, alignment=1, spaceAfter=12))
    styles.add(ParagraphStyle(name='SubHeading', fontName='Helvetica-Bold', fontSize=10))
    
    # Create a table for the header with company info on left and payslip info on right
    header_data = []
    
    # Left column - Company info
    company_info = []
    if company and company.logo:
        logo_path = os.path.join(app.static_folder, company.logo)
        if os.path.exists(logo_path):
            img = Image(logo_path)
            img.drawHeight = 60
            img.drawWidth = 120
            company_info.append(img)
    
    if company:
        company_info.append(Paragraph(f"<b>{company.name}</b>", styles['Heading3']))
        company_info.append(Paragraph(company.address, styles['Normal']))
        company_info.append(Paragraph(f"Phone: {company.phone}", styles['Normal']))
        company_info.append(Paragraph(f"Email: {company.email}", styles['Normal']))
        company_info.append(Paragraph(f"Registration #: {company.registration_number}", styles['Normal']))
        company_info.append(Paragraph(f"Tax #: {company.tax_number}", styles['Normal']))
    
    # Right column - Payslip info with improved styling
    payslip_info = []
    payslip_info.append(Paragraph("<font color='blue'><b>PAYSLIP</b></font>", styles['PayslipTitle']))
    
    payslip_info.append(Paragraph(f"For the month of <b>{month_name}</b>", styles['Normal']))
    payslip_info.append(Paragraph(f"Pay Date: <b>{payroll_record.pay_date}</b>", styles['Normal']))
    
    # Add company and payslip info to the header table
    header_data.append([company_info, payslip_info])
    
    # Create the header table with better spacing
    header_table = Table(header_data, colWidths=[doc.width/2.0]*2)
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 20))
    
    # Employee Information in a styled box with improved colors
    elements.append(Paragraph("<b>Employee Details</b>", styles['Heading4']))
    employee_data = [
        [Paragraph(f"<b>Name:</b> {payroll_record.employee.name}", styles['Normal']), 
         Paragraph(f"<b>Employee Code:</b> {payroll_record.employee.employee_code}", styles['Normal'])],
        [Paragraph(f"<b>Position:</b> {payroll_record.employee.position}", styles['Normal']), 
         Paragraph(f"<b>Email:</b> {payroll_record.employee.email}", styles['Normal'])],
        [Paragraph(f"<b>Department:</b> {payroll_record.employee.department}", styles['Normal']), 
         Paragraph(f"<b>Branch:</b> {payroll_record.employee.branch.name if payroll_record.employee.branch else ''}", styles['Normal'])]
    ]
    # Match the employee table width to the combined table width that will be used later
    employee_table = Table(employee_data, colWidths=[doc.width/2.0]*2)
    employee_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(employee_table)
    elements.append(Spacer(1, 20))
    
    # Create tables for earnings and deductions side by side with modern colors
    earnings_data = [
        [Paragraph("<b>Earnings</b>", styles['SubHeading']), Paragraph("<b>Amount</b>", styles['Right'])],
        ['Basic Salary', Paragraph(f"K {payroll_record.basic_salary:,.2f}", styles['Right'])],
        ['Housing Allowance', Paragraph(f"K {payroll_record.housing_allowance:,.2f}", styles['Right'])],
        ['Lunch Allowance', Paragraph(f"K {payroll_record.lunch_allowance:,.2f}", styles['Right'])],
        ['Transport Allowance', Paragraph(f"K {payroll_record.transport_allowance:,.2f}", styles['Right'])],
        ['Overtime', Paragraph(f"K {payroll_record.overtime:,.2f}", styles['Right'])],
        [Paragraph("<b>Gross Pay:</b>", styles['SubHeading']), Paragraph(f"<b>K {payroll_record.gross_pay:,.2f}</b>", styles['Right'])]
    ]
    
    deductions_data = [
        [Paragraph("<b>Deductions</b>", styles['SubHeading']), Paragraph("<b>Amount</b>", styles['Right'])],
        ['PAYE', Paragraph(f"K {payroll_record.paye:,.2f}", styles['Right'])],
        ['NAPSA', Paragraph(f"K {payroll_record.napsa:,.2f}", styles['Right'])],
        ['NHIMA', Paragraph(f"K {payroll_record.nhima:,.2f}", styles['Right'])],
        ['Salary Advance', Paragraph(f"K {payroll_record.salary_advance:,.2f}", styles['Right'])],
        ['Rainbow Loan', Paragraph(f"K {payroll_record.rainbow_loan:,.2f}", styles['Right'])],
        ['Other Deduction', Paragraph(f"K {payroll_record.other_deduction:,.2f}", styles['Right'])],
        [Paragraph("<b>Total Deductions:</b>", styles['SubHeading']), Paragraph(f"<b>K {payroll_record.total_deductions:,.2f}</b>", styles['Right'])]
    ]
    
    # Table styles with modern colors
    earnings_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.green),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, -1), (-1, -1), colors.lightgreen),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 8),
    ])
    
    deductions_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.red),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, -1), (-1, -1), colors.pink),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 8),
    ])
    
    # Create the tables with widths that align with the employee table
    # Use 45% of doc width for content and 5% for spacing on each table
    earnings_table = Table(earnings_data, colWidths=[(doc.width*0.45)*0.7, (doc.width*0.45)*0.3])
    earnings_table.setStyle(earnings_style)
    
    deductions_table = Table(deductions_data, colWidths=[(doc.width*0.45)*0.7, (doc.width*0.45)*0.3])
    deductions_table.setStyle(deductions_style)
    
    # Create a table to hold both earnings and deductions side by side with spacing between
    combined_data = [[earnings_table, deductions_table]]
    combined_table = Table(combined_data, colWidths=[doc.width*0.5, doc.width*0.5])
    combined_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (0, 0), 0),  # No padding on left side of earnings table
        ('RIGHTPADDING', (0, 0), (0, 0), doc.width*0.05),  # 5% spacing between tables
        ('LEFTPADDING', (1, 0), (1, 0), doc.width*0.05),  # 5% spacing between tables
        ('RIGHTPADDING', (1, 0), (1, 0), 0),  # No padding on right side of deductions table
    ]))
    elements.append(combined_table)
    
    elements.append(Spacer(1, 25))
    
    # Net Pay in a highlighted box with improved styling
    net_pay_data = [[Paragraph("<font color='white'><b>Net Pay</b></font>", styles['Heading3']), 
                    Paragraph(f"<font color='white'><b>K {payroll_record.net_pay:,.2f}</b></font>", styles['Heading2'])]]
    net_pay_table = Table(net_pay_data, colWidths=[doc.width/2.0, doc.width/2.0])
    net_pay_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.blue),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (0, 0), 14),
        ('FONTSIZE', (1, 0), (1, 0), 18),
        ('PADDING', (0, 0), (-1, -1), 15),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    elements.append(net_pay_table)
    
    # Add footer with date and signature
    elements.append(Spacer(1, 30))
    footer_text = f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M')} | This is a computer-generated document and requires no signature."
    elements.append(Paragraph(footer_text, styles['Normal']))
    
    # Build PDF
    doc.build(elements)
    
    # Get the PDF content
    pdf_content = pdf_buffer.getvalue()
    pdf_buffer.close()
    
    # Prepare email content
    subject = f"Payslip for {month_name}"
    html_body = f"""
    <html>
    <body>
        <p>Dear Sir/Madam,</p>
        <p>Please find attached the payslip for {employee.name} (Employee Code: {employee.employee_code}) for {month_name}.</p>
        <p>If you have any questions regarding this payslip, please contact the HR department.</p>
        <p>Best regards,<br>{company.name} Payroll Team</p>
    </body>
    </html>
    """
    
    # Prepare attachment
    filename = f"Payslip_{employee.employee_code}_{month_name.replace(' ', '_')}.pdf"
    
    # Get email settings
    email_settings = EmailSettings.query.first()
    if not email_settings or not email_settings.enabled:
        return False, "Email not configured or disabled"
    
    # Create a list of recipients with just the employee's email
    recipients = [custom_email] if custom_email else [employee.email]
    
    # Create attachment tuple in the format expected by the send_email function
    attachments = [(filename, pdf_content)]
    
    # Send email using the proper send_email function that takes subject, recipients, html_body, attachments
    return send_email(subject, recipients, html_body, attachments)

@app.route('/download_payslip/<int:record_id>')
@login_required
def download_payslip(record_id):
    record = PayrollRecord.query.get_or_404(record_id)
    company = Company.query.first()
    
    # Create a BytesIO buffer for the PDF
    buffer = BytesIO()
    
    # Create the PDF document with better styling
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Center', alignment=1))
    styles.add(ParagraphStyle(name='Right', alignment=2))
    styles.add(ParagraphStyle(name='PayslipTitle', fontName='Helvetica-Bold', fontSize=16, alignment=1, spaceAfter=12))
    styles.add(ParagraphStyle(name='SubHeading', fontName='Helvetica-Bold', fontSize=10))
    
    # Create a table for the header with company info on left and payslip info on right
    header_data = []
    
    # Left column - Company info
    company_info = []
    if company and company.logo:
        logo_path = os.path.join(app.static_folder, company.logo)
        if os.path.exists(logo_path):
            img = Image(logo_path)
            img.drawHeight = 60
            img.drawWidth = 120
            company_info.append(img)
    
    if company:
        company_info.append(Paragraph(f"<b>{company.name}</b>", styles['Heading3']))
        company_info.append(Paragraph(company.address, styles['Normal']))
        company_info.append(Paragraph(f"Phone: {company.phone}", styles['Normal']))
        company_info.append(Paragraph(f"Email: {company.email}", styles['Normal']))
        company_info.append(Paragraph(f"Registration #: {company.registration_number}", styles['Normal']))
        company_info.append(Paragraph(f"Tax #: {company.tax_number}", styles['Normal']))
    
    # Right column - Payslip info with improved styling
    payslip_info = []
    payslip_info.append(Paragraph("<font color='blue'><b>PAYSLIP</b></font>", styles['PayslipTitle']))
    
    # Format the pay date properly
    pay_month = ""
    try:
        if isinstance(record.pay_date, str):
            # Try to parse the date string
            pay_date = datetime.strptime(record.pay_date, '%Y-%m-%d')
            pay_month = pay_date.strftime('%B %Y')
        else:
            pay_month = record.pay_date.strftime('%B %Y')
    except:
        pay_month = record.pay_date[0:7]
    
    payslip_info.append(Paragraph(f"For the month of <b>{pay_month}</b>", styles['Normal']))
    payslip_info.append(Paragraph(f"Pay Date: <b>{record.pay_date}</b>", styles['Normal']))
    
    # Add company and payslip info to the header table
    header_data.append([company_info, payslip_info])
    
    # Create the header table with better spacing
    header_table = Table(header_data, colWidths=[doc.width/2.0]*2)
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 20))
    
    # Employee Information in a styled box with improved colors
    elements.append(Paragraph("<b>Employee Details</b>", styles['Heading4']))
    employee_data = [
        [Paragraph(f"<b>Name:</b> {record.employee.name}", styles['Normal']), 
         Paragraph(f"<b>Employee Code:</b> {record.employee.employee_code}", styles['Normal'])],
        [Paragraph(f"<b>Position:</b> {record.employee.position}", styles['Normal']), 
         Paragraph(f"<b>Email:</b> {record.employee.email}", styles['Normal'])],
        [Paragraph(f"<b>Department:</b> {record.employee.department}", styles['Normal']), 
         Paragraph(f"<b>Branch:</b> {record.employee.branch.name if record.employee.branch else ''}", styles['Normal'])]
    ]
    # Match the employee table width to the combined table width that will be used later
    employee_table = Table(employee_data, colWidths=[doc.width/2.0]*2)
    employee_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(employee_table)
    elements.append(Spacer(1, 20))
    
    # Create tables for earnings and deductions side by side with modern colors
    earnings_data = [
        [Paragraph("<b>Earnings</b>", styles['SubHeading']), Paragraph("<b>Amount</b>", styles['Right'])],
        ['Basic Salary', Paragraph(f"K {record.basic_salary:,.2f}", styles['Right'])],
        ['Housing Allowance', Paragraph(f"K {record.housing_allowance:,.2f}", styles['Right'])],
        ['Lunch Allowance', Paragraph(f"K {record.lunch_allowance:,.2f}", styles['Right'])],
        ['Transport Allowance', Paragraph(f"K {record.transport_allowance:,.2f}", styles['Right'])],
        ['Overtime', Paragraph(f"K {record.overtime:,.2f}", styles['Right'])],
        [Paragraph("<b>Gross Pay:</b>", styles['SubHeading']), Paragraph(f"<b>K {record.gross_pay:,.2f}</b>", styles['Right'])]
    ]
    
    deductions_data = [
        [Paragraph("<b>Deductions</b>", styles['SubHeading']), Paragraph("<b>Amount</b>", styles['Right'])],
        ['PAYE', Paragraph(f"K {record.paye:,.2f}", styles['Right'])],
        ['NAPSA', Paragraph(f"K {record.napsa:,.2f}", styles['Right'])],
        ['NHIMA', Paragraph(f"K {record.nhima:,.2f}", styles['Right'])],
        ['Salary Advance', Paragraph(f"K {record.salary_advance:,.2f}", styles['Right'])],
        ['Rainbow Loan', Paragraph(f"K {record.rainbow_loan:,.2f}", styles['Right'])],
        ['Other Deduction', Paragraph(f"K {record.other_deduction:,.2f}", styles['Right'])],
        [Paragraph("<b>Total Deductions:</b>", styles['SubHeading']), Paragraph(f"<b>K {record.total_deductions:,.2f}</b>", styles['Right'])]
    ]
    
    # Table styles with modern colors
    earnings_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.green),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, -1), (-1, -1), colors.lightgreen),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 8),
    ])
    
    deductions_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.red),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, -1), (-1, -1), colors.pink),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 8),
    ])
    
    # Create the tables with widths that align with the employee table
    # Use 45% of doc width for content and 5% for spacing on each table
    earnings_table = Table(earnings_data, colWidths=[(doc.width*0.45)*0.7, (doc.width*0.45)*0.3])
    earnings_table.setStyle(earnings_style)
    
    deductions_table = Table(deductions_data, colWidths=[(doc.width*0.45)*0.7, (doc.width*0.45)*0.3])
    deductions_table.setStyle(deductions_style)
    
    # Create a table to hold both earnings and deductions side by side with spacing between
    combined_data = [[earnings_table, deductions_table]]
    combined_table = Table(combined_data, colWidths=[doc.width*0.5, doc.width*0.5])
    combined_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (0, 0), 0),  # No padding on left side of earnings table
        ('RIGHTPADDING', (0, 0), (0, 0), doc.width*0.05),  # 5% spacing between tables
        ('LEFTPADDING', (1, 0), (1, 0), doc.width*0.05),  # 5% spacing between tables
        ('RIGHTPADDING', (1, 0), (1, 0), 0),  # No padding on right side of deductions table
    ]))
    elements.append(combined_table)
    
    elements.append(Spacer(1, 25))
    
    # Net Pay in a highlighted box with improved styling
    net_pay_data = [[Paragraph("<font color='white'><b>Net Pay</b></font>", styles['Heading3']), 
                    Paragraph(f"<font color='white'><b>K {record.net_pay:,.2f}</b></font>", styles['Heading2'])]]
    net_pay_table = Table(net_pay_data, colWidths=[doc.width/2.0, doc.width/2.0])
    net_pay_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.blue),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (0, 0), 14),
        ('FONTSIZE', (1, 0), (1, 0), 18),
        ('PADDING', (0, 0), (-1, -1), 15),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    elements.append(net_pay_table)
    
    # Add footer with date and signature
    elements.append(Spacer(1, 30))
    footer_text = f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M')} | This is a computer-generated document and requires no signature."
    elements.append(Paragraph(footer_text, styles['Normal']))
    
    # Build PDF
    doc.build(elements)
    
    # Get the PDF content
    pdf_content = buffer.getvalue()
    buffer.close()
    
    # Create a response with the PDF content
    response = make_response(pdf_content)
    response.headers['Content-Type'] = 'application/pdf'
    
    # Check if this is a download request or just viewing
    if request.args.get('download') == 'true':
        # For downloading, set as attachment
        response.headers['Content-Disposition'] = f'attachment; filename=payslip_{record.employee.employee_code}_{pay_month.replace(" ", "_")}.pdf'
    else:
        # For viewing in browser, set as inline
        response.headers['Content-Disposition'] = f'inline; filename=payslip_{record.employee.employee_code}_{pay_month.replace(" ", "_")}.pdf'
    
    return response
@app.route('/delete_payroll_record/<int:record_id>', methods=['POST'])
@login_required
def delete_payroll_record(record_id):
    record = PayrollRecord.query.get_or_404(record_id)
    try:
        db.session.delete(record)
        db.session.commit()
        flash('Payroll record deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Error deleting payroll record.', 'error')
    
    return redirect(url_for('payroll_records'))

@app.route('/delete_selected_records', methods=['POST'])
@login_required
def delete_selected_records():
    record_ids = request.form.getlist('record_ids[]')
    if not record_ids:
        flash('No records selected for deletion.', 'warning')
        return redirect(url_for('payroll_records'))

    try:
        # Delete selected records
        PayrollRecord.query.filter(PayrollRecord.id.in_(record_ids)).delete(synchronize_session=False)
        db.session.commit()
        flash(f'Successfully deleted {len(record_ids)} payroll record(s).', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Error deleting records. Please try again.', 'error')

    return redirect(url_for('payroll_records'))

@app.route('/company', methods=['GET', 'POST'])
@login_required
def company_settings():
    company = Company.query.first()
    branches = Branch.query.all()
    
    # Get list of existing images in the uploads folder
    upload_folder = os.path.join(app.static_folder, 'static', 'uploads')
    existing_images = []
    if os.path.exists(upload_folder):
        for filename in os.listdir(upload_folder):
            if allowed_file(filename):
                existing_images.append({
                    'filename': filename,
                    'path': f'uploads/{filename}',
                    'url': url_for('static', filename=f'uploads/{filename}')
                })
    
    if request.method == 'POST':
        # Check if we're handling branch operations
        if 'branch_action' in request.form:
            branch_action = request.form.get('branch_action')
            
            # Add new branch
            if branch_action == 'add':
                branch_name = request.form.get('branch_name')
                branch_address = request.form.get('branch_address')
                branch_phone = request.form.get('branch_phone')
                branch_email = request.form.get('branch_email')
                branch_manager = request.form.get('branch_manager')
                
                # Validate branch name
                if not branch_name:
                    flash('Branch name is required.', 'error')
                    return redirect(url_for('company_settings'))
                
                # Check if branch with same name already exists
                existing_branch = Branch.query.filter_by(name=branch_name).first()
                if existing_branch:
                    flash('A branch with this name already exists.', 'error')
                    return redirect(url_for('company_settings'))
                
                # Create new branch
                new_branch = Branch(
                    name=branch_name,
                    address=branch_address,
                    phone=branch_phone,
                    email=branch_email,
                    manager=branch_manager,
                    is_active=True
                )
                
                try:
                    db.session.add(new_branch)
                    db.session.commit()
                    flash('Branch added successfully!', 'success')
                except Exception as e:
                    db.session.rollback()
                    flash('Error adding branch. Please try again.', 'error')
                
                return redirect(url_for('company_settings'))
            
            # Edit existing branch
            elif branch_action == 'edit':
                branch_id = request.form.get('branch_id')
                branch = Branch.query.get_or_404(branch_id)
                
                branch.name = request.form.get('branch_name')
                branch.address = request.form.get('branch_address')
                branch.phone = request.form.get('branch_phone')
                branch.email = request.form.get('branch_email')
                branch.manager = request.form.get('branch_manager')
                branch.is_active = 'branch_active' in request.form
                
                try:
                    db.session.commit()
                    flash('Branch updated successfully!', 'success')
                except Exception as e:
                    db.session.rollback()
                    flash('Error updating branch. Please try again.', 'error')
                
                return redirect(url_for('company_settings'))
            
            # Delete branch
            elif branch_action == 'delete':
                branch_id = request.form.get('branch_id')
                branch = Branch.query.get_or_404(branch_id)
                
                # Check if branch has employees
                if Employee.query.filter_by(branch_id=branch.id).count() > 0:
                    flash('Cannot delete branch with assigned employees.', 'error')
                    return redirect(url_for('company_settings'))
                
                try:
                    db.session.delete(branch)
                    db.session.commit()
                    flash('Branch deleted successfully!', 'success')
                except Exception as e:
                    db.session.rollback()
                    flash('Error deleting branch. Please try again.', 'error')
                
                return redirect(url_for('company_settings'))
        
        # Handle company information update
        else:
            # Check if any company information fields have been modified
            has_company_info_changed = False
            
            # Get form data
            name = request.form.get('name')
            address = request.form.get('address')
            phone = request.form.get('phone')
            email = request.form.get('email')
            website = request.form.get('website')
            registration_number = request.form.get('registration_number')
            tax_number = request.form.get('tax_number')
            
            if company is None:
                company = Company()
                db.session.add(company)
                has_company_info_changed = True
            else:
                # Check if any fields have changed
                if (company.name != name or
                    company.address != address or
                    company.phone != phone or
                    company.email != email or
                    company.website != website or
                    company.registration_number != registration_number or
                    company.tax_number != tax_number):
                    has_company_info_changed = True
            
            # Only update company information if it has changed
            if has_company_info_changed:
                company.name = name
                company.address = address
                company.phone = phone
                company.email = email
                company.website = website
                company.registration_number = registration_number
                company.tax_number = tax_number
            
            # Handle logo selection or upload
            logo_changed = False
            selected_existing_image = request.form.get('existing_logo')
            
            if selected_existing_image and selected_existing_image != 'none':
                # User selected an existing image
                if company.logo != selected_existing_image:
                    company.logo = selected_existing_image
                    logo_changed = True
            elif 'logo' in request.files:
                # User uploaded a new image
                file = request.files['logo']
                if file and file.filename and allowed_file(file.filename):
                    logo_changed = True
                    # Delete old logo if it exists and it's not the selected one
                    if company.logo and company.logo != selected_existing_image:
                        old_logo_path = os.path.join(app.root_path, 'static', company.logo)
                        if os.path.exists(old_logo_path):
                            os.remove(old_logo_path)
                    
                    # Save new logo
                    filename = secure_filename(file.filename)
                    # Add timestamp to filename to prevent caching issues
                    filename = f"{int(datetime.now().timestamp())}_{filename}"
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(file_path)
                    company.logo = f"uploads/{filename}"  # Store relative path from static directory
            
            try:
                # Only commit if there were actual changes
                if has_company_info_changed or logo_changed:
                    db.session.commit()
                    if has_company_info_changed:
                        flash('Company information updated successfully!', 'success')
                    elif logo_changed:
                        flash('Company logo updated successfully!', 'success')
                else:
                    flash('No changes were made to company information.', 'info')
            except Exception as e:
                db.session.rollback()
                flash(f'Error updating company information: {str(e)}', 'error')
    
    return render_template('company_settings.html', company=company, branches=branches, existing_images=existing_images)

@app.route('/employees')
@login_required
def employees():
    employees = Employee.query.all()
    return render_template('employees.html', employees=employees)



def safe_float(value, default=0.0):
    # Convert a value to float safely, returning default if conversion fails.
    if value is None or value == '':
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

@app.route('/add_employee', methods=['GET', 'POST'])
@login_required
def add_employee():
    if request.method == 'POST':
        # Generate a unique employee code
        last_employee = Employee.query.order_by(Employee.id.desc()).first()
        if last_employee:
            last_code = last_employee.employee_code
            if last_code.startswith('EMP'):
                try:
                    code_num = int(last_code[3:]) + 1
                    employee_code = f'EMP{code_num:04d}'
                except ValueError:
                    employee_code = f'EMP{1:04d}'
        else:
            employee_code = f'EMP{1:04d}'
        
        # Get branch if selected
        branch_id = request.form.get('branch')
        branch = None
        if branch_id and branch_id != 'none':
            branch = Branch.query.get(branch_id)
        
        # Create new employee with safe handling of blank fields
        employee = Employee(
            employee_code=employee_code,
            name=request.form.get('name', ''),
            nrc=request.form.get('nrc', ''),
            email=request.form.get('email', ''),
            position=request.form.get('position', ''),
            department=request.form.get('department', ''),
            basic_salary=safe_float(request.form.get('basic_salary')),
            housing_allowance=safe_float(request.form.get('housing_allowance')),
            lunch_allowance=safe_float(request.form.get('lunch_allowance')),
            transport_allowance=safe_float(request.form.get('transport_allowance')),
            overtime=safe_float(request.form.get('overtime')),
            salary_advance=safe_float(request.form.get('salary_advance')),
            rainbow_loan=safe_float(request.form.get('rainbow_loan')),
            other_deduction=safe_float(request.form.get('other_deduction')),
            other_deduction_reason=request.form.get('other_deduction_reason', ''),
            branch=branch
        )
        
        db.session.add(employee)
        db.session.commit()
        
        flash(f'Employee {employee.name} added successfully!', 'success')
        return redirect(url_for('employees'))
    
    # Get all branches for the dropdown
    branches = Branch.query.all()
    return render_template('add_employee.html', branches=branches)

@app.route('/edit_employee/<int:employee_id>', methods=['GET', 'POST'])
@login_required
def edit_employee(employee_id):
    employee = Employee.query.get_or_404(employee_id)
    
    def safe_float(val):
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0
    
    if request.method == 'POST':
        try:
            # Validate required fields
            name = request.form.get('name', '').strip()
            nrc = request.form.get('nrc', '').strip()
            if not name or not nrc:
                flash('Employee name and NRC are required.', 'error')
                branches = Branch.query.all()
                return render_template('edit_employee.html', employee=employee, branches=branches)

            employee.name = name
            employee.nrc = nrc
            employee.email = request.form.get('email')
            employee.position = request.form.get('position')
            employee.department = request.form.get('department')
            employee.basic_salary = safe_float(request.form.get('basic_salary'))
            employee.housing_allowance = safe_float(request.form.get('housing_allowance'))
            employee.lunch_allowance = safe_float(request.form.get('lunch_allowance'))
            employee.transport_allowance = safe_float(request.form.get('transport_allowance'))
            employee.overtime = safe_float(request.form.get('overtime'))
            employee.salary_advance = safe_float(request.form.get('salary_advance'))
            employee.rainbow_loan = safe_float(request.form.get('rainbow_loan'))
            employee.other_deduction = safe_float(request.form.get('other_deduction'))
            employee.other_deduction_reason = request.form.get('other_deduction_reason', '')

            # Update branch if selected
            branch_id = request.form.get('branch')
            if branch_id and branch_id != 'none':
                branch = Branch.query.get(branch_id)
                employee.branch = branch
            else:
                employee.branch = None

            # Update active status
            employee.is_active = 'is_active' in request.form

            db.session.commit()
            flash(f'Employee {employee.name} updated successfully!', 'success')
            return redirect(url_for('employees'))
        except Exception as e:
            import traceback
            db.session.rollback()
            error_details = traceback.format_exc()
            print("[DEBUG][edit_employee] Exception occurred:\n", error_details)
            flash(f'Error updating employee: {str(e)}', 'error')
            branches = Branch.query.all()
            return render_template('edit_employee.html', employee=employee, branches=branches)
    
    # Get all branches for the dropdown
    branches = Branch.query.all()
    return render_template('edit_employee.html', employee=employee, branches=branches)

@app.route('/delete_employee/<int:employee_id>', methods=['POST'])
@login_required
def delete_employee(employee_id):
    employee = Employee.query.get_or_404(employee_id)
    try:
        # Check if employee has payroll records
        has_records = PayrollRecord.query.filter_by(employee_id=employee_id).first() is not None
        if has_records:
            flash(f'Cannot delete employee {employee.name} because they have payroll records.', 'danger')
            return redirect(url_for('employees'))
            
        db.session.delete(employee)
        db.session.commit()
        flash(f'Employee {employee.name} has been deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting employee: {str(e)}', 'danger')
    
    return redirect(url_for('employees'))

@app.route('/tax_settings', methods=['GET', 'POST'])
@login_required
def tax_settings():
    if request.method == 'POST':
        # Update tax settings in the database
        tax_settings = TaxSettings.query.first()
        if not tax_settings:
            tax_settings = TaxSettings()
            db.session.add(tax_settings)
        
        # Update PAYE brackets
        tax_settings.bracket1 = float(request.form.get('bracket1', 0))    # 0% up to K5,100
        tax_settings.bracket2 = float(request.form.get('bracket2', 20))   # 20% K5,100.01 - K7,100
        tax_settings.bracket3 = float(request.form.get('bracket3', 30))   # 30% K7,100.01 - K9,200
        tax_settings.bracket4 = float(request.form.get('bracket4', 37))   # 37% above K9,200
        
        db.session.commit()
        flash('Tax settings updated successfully!', 'success')
        return redirect(url_for('tax_settings'))
    
    # Get current tax settings
    tax_settings = TaxSettings.query.first()
    if not tax_settings:
        tax_settings = TaxSettings(
            bracket1=0,    # 0% up to K5,100
            bracket2=20,   # 20% K5,100.01 - K7,100
            bracket3=30,   # 30% K7,100.01 - K9,200
            bracket4=37    # 37% above K9,200
        )
        db.session.add(tax_settings)
        db.session.commit()
    
    return render_template('tax_settings.html', settings=tax_settings)

@app.route('/email_settings', methods=['GET', 'POST'])
@login_required
def email_settings():
    """Manage email settings"""
    if not current_user.is_admin:
        flash('Permission denied', 'danger')
        return redirect(url_for('dashboard'))
    
    # Get or create email settings
    email_settings = EmailSettings.query.first()
    if not email_settings:
        email_settings = EmailSettings(
            smtp_server='smtp.gmail.com',
            smtp_port=587,
            smtp_use_tls=True,
            smtp_username=None,
            smtp_password=None,
            default_sender=None,
            enabled=False
        )
        db.session.add(email_settings)
        db.session.commit()
    
    # Get company for email templates
    company = Company.query.first()
    
    if request.method == 'POST':
        if 'save_settings' in request.form:
            # Update email settings
            email_settings.smtp_server = request.form.get('smtp_server')
            email_settings.smtp_port = int(request.form.get('smtp_port'))
            email_settings.smtp_use_tls = 'use_tls' in request.form
            email_settings.smtp_username = request.form.get('username')
            
            # Only update password if provided
            new_password = request.form.get('password')
            if new_password and new_password.strip():
                email_settings.smtp_password = new_password
                
            email_settings.default_sender = request.form.get('default_sender')
            email_settings.enabled = 'enabled' in request.form
            
            # Update company email templates
            company.email_signature = request.form.get('email_signature')
            company.email_footer = request.form.get('email_footer')
            
            db.session.commit()
            
            # Apply settings to Flask-Mail
            email_settings.apply_to_app()
            
            flash('Email settings updated successfully', 'success')
        
        elif 'test_email' in request.form:
            # Send test email
            test_recipient = request.form.get('test_recipient')
            if not test_recipient or '@' not in test_recipient:
                flash('Please provide a valid email address for testing', 'danger')
            else:
                try:
                    # Log email settings for debugging
                    print(f"\nEmail Test Settings:")
                    print(f"SMTP Server: {email_settings.smtp_server}:{email_settings.smtp_port}")
                    print(f"TLS Enabled: {email_settings.smtp_use_tls}")
                    print(f"Username: {email_settings.smtp_username}")
                    print(f"Default Sender: {email_settings.default_sender}")
                    print(f"Recipient: {test_recipient}\n")
                    
                    # Create test email
                    subject = "Test Email from Payroll System"
                    html_body = f"""
                    <html>
                    <body>
                        <h2>Test Email from Payroll System</h2>
                        <p>This is a test email from your payroll system.</p>
                        <hr>
                        {company.email_signature or ''}
                        {company.email_footer or ''}
                    </body>
                    </html>
                    """
                    
                    # Send email using the utility function
                    success, error_message = send_smtp_email(
                        smtp_server=email_settings.smtp_server,
                        smtp_port=email_settings.smtp_port,
                        use_tls=email_settings.smtp_use_tls,
                        username=email_settings.smtp_username,
                        password=email_settings.smtp_password,
                        sender=email_settings.default_sender or email_settings.smtp_username,
                        recipients=test_recipient,
                        subject=subject,
                        html_body=html_body
                    )
                    
                    if success:
                        # Update test status
                        email_settings.last_tested = datetime.now()
                        email_settings.test_status = 'Success'
                        db.session.commit()
                        
                        flash('Test email sent successfully', 'success')
                    else:
                        raise Exception(error_message)
                        
                except Exception as e:
                    # Update test status with detailed error
                    error_details = str(e)
                    import traceback
                    error_trace = traceback.format_exc()
                    print(f"Email Test Error: {error_details}")
                    print(error_trace)
                    
                    email_settings.last_tested = datetime.now()
                    email_settings.test_status = f'Failed: {error_details}'
                    db.session.commit()
                    
                    flash(f'Error sending test email: {error_details}', 'danger')
                    flash('Check server logs for more details', 'warning')
    
    return render_template('email_settings.html', email_settings=email_settings, company=company)

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    # Handle profile updates
    if request.method == 'POST' and 'update_profile' in request.form:
        # Update current user's profile
        current_user.username = request.form.get('username')
        
        # Handle password change if provided
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        if current_password and new_password and confirm_password:
            if not check_password_hash(current_user.password, current_password):
                flash('Current password is incorrect', 'danger')
                return redirect(url_for('profile'))
            
            if new_password != confirm_password:
                flash('New passwords do not match', 'danger')
                return redirect(url_for('profile'))
            
            current_user.password = generate_password_hash(new_password)
            flash('Password updated successfully', 'success')
        
        db.session.commit()
        flash('Profile updated successfully', 'success')
        return redirect(url_for('profile'))
    
    # Handle new user creation (admin only)
    if request.method == 'POST' and 'create_user' in request.form and current_user.is_admin:
        username = request.form.get('new_username')
        password = request.form.get('new_password')
        is_admin = 'is_admin' in request.form
        
        # Check if username already exists
        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'danger')
            return redirect(url_for('profile'))
        
        # Create new user
        new_user = User(
            username=username, 
            password=generate_password_hash(password), 
            is_admin=is_admin,
            created_at=datetime.utcnow()
        )
        db.session.add(new_user)
        db.session.commit()
        flash(f'User {username} created successfully', 'success')
        return redirect(url_for('profile'))
    
    # Get all users for admin view
    users = []
    if current_user.is_admin:
        users = User.query.all()
    
    return render_template('profile.html', users=users)

@app.route('/favicon-generator')
def favicon_generator():
    return render_template('favicon_generator.html')

def calculate_paye(gross_pay):
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

@app.route('/payroll_reports', methods=['GET', 'POST'])
@login_required
def payroll_reports():
    # Initialize variables
    year = datetime.now().year
    month = datetime.now().month
    report_type = 'monthly'
    report_format = 'pdf'
    reports = []
    generate_report = False
    
    if request.method == 'POST':
        # Get form data
        year = request.form.get('year', datetime.now().year, type=int)
        month = request.form.get('month', type=int)  # Month can be None for annual reports
        report_type = request.form.get('report_type', 'monthly')
        report_format = request.form.get('format', 'pdf')
        generate_report = True  # Only generate when form is submitted
    else:
        # Get query parameters for GET requests
        year = request.args.get('year', datetime.now().year, type=int)
        month = request.args.get('month', datetime.now().month, type=int)
        report_type = request.args.get('type', 'monthly')
        report_format = request.args.get('format', 'pdf')
        generate_report = request.args.get('generate', 'false').lower() == 'true'
    
    # Get existing reports from database
    existing_reports = Report.query.order_by(Report.generated_at.desc()).all()
    
    # Create reports directory if it doesn't exist
    reports_dir = os.path.join(app.static_folder, 'reports')
    os.makedirs(reports_dir, exist_ok=True)

    # Only generate a new report if explicitly requested
    if generate_report:
        current_time = datetime.now()
        current_time_str = current_time.strftime('%Y-%m-%d %H:%M:%S')
        
        # Get payroll records based on report type
        if report_type == 'monthly' and month:
            records = PayrollRecord.query.filter(
                extract('year', PayrollRecord.pay_date) == year,
                extract('month', PayrollRecord.pay_date) == month
            ).all()
            
            # Process monthly records
            total_basic = sum(record.basic_salary for record in records)
            total_housing_allowance = sum(record.housing_allowance for record in records)
            total_lunch_allowance = sum(record.lunch_allowance for record in records)
            total_transport_allowance = sum(record.transport_allowance for record in records)
            total_overtime = sum(record.overtime for record in records)
            total_gross_pay = sum(record.gross_pay for record in records)
            total_paye = sum(record.paye for record in records)
            total_napsa = sum(record.napsa for record in records)
            total_nhima = sum(record.nhima for record in records)
            total_deductions = sum(record.total_deductions for record in records)
            total_net = sum(record.net_pay for record in records)
            
            report_name = f"Monthly Payroll Report - {get_month_name(month)} {year}"
            report_id = f"monthly_{year}_{month}_{int(current_time.timestamp())}"
            file_path = f"reports/{report_id}.pdf"
            report_path = os.path.join(app.static_folder, file_path)

            # Only allow PDF generation
            # Generate the PDF file
            doc = SimpleDocTemplate(
                report_path,
                pagesize=(landscape(letter)[0] + 1.5*inch, landscape(letter)[1] + 0.5*inch),
                rightMargin=10,
                leftMargin=10,
                topMargin=20,
                bottomMargin=20
            )
            elements = []
            styles = getSampleStyleSheet()
            
            # Add title
            title_style = ParagraphStyle(
                'CustomTitle',
                parent=styles['Heading1'],
                fontSize=12,
                spaceAfter=6,
                alignment=1
            )
            title = Paragraph(f"Monthly Payroll Report - {get_month_name(month)} {year}", title_style)
            elements.append(title)
            
            # Add company information
            company = Company.query.first()
            if company:
                company_style = ParagraphStyle(
                    'CompanyInfo',
                    parent=styles['Normal'],
                    fontSize=8,
                    leading=10,
                    alignment=1
                )
                elements.append(Paragraph(company.name, company_style))
                elements.append(Paragraph(company.address, company_style))
                elements.append(Paragraph(f"Phone: {company.phone}", company_style))
                elements.append(Paragraph(f"Email: {company.email}", company_style))
            
            elements.append(Spacer(1, 20))
            
            # Add 'Summary' heading centered
            summary_heading_style = ParagraphStyle(
                'SummaryHeading',
                parent=styles['Heading2'],
                alignment=1
            )
            elements.append(Paragraph("Summary", summary_heading_style))
            elements.append(Spacer(1, 8))
            
            # Add summary table
            summary_data = [
                ['Total Employees', len(records)],
                ['Total Basic Salary', f"K {total_basic:,.2f}"],
                ['Total Housing Allowance', f"K {total_housing_allowance:,.2f}"],
                ['Total Lunch Allowance', f"K {total_lunch_allowance:,.2f}"],
                ['Total Transport Allowance', f"K {total_transport_allowance:,.2f}"],
                ['Total Overtime', f"K {total_overtime:,.2f}"],
                ['Total Gross Pay', f"K {total_gross_pay:,.2f}"],
                ['Total PAYE', f"K {total_paye:,.2f}"],
                ['Total NAPSA', f"K {total_napsa:,.2f}"],
                ['Total NHIMA', f"K {total_nhima:,.2f}"],
                ['Total Deductions', f"K {total_deductions:,.2f}"],
                ['Total Net Pay', f"K {total_net:,.2f}"]
            ]
            
            summary_table = Table(summary_data, colWidths=[200, 200])
            summary_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 1, colors.black)
            ]))
            elements.append(summary_table)
            
            # Add employee details table
            elements.append(Spacer(1, 20))
            # Centered 'Employee Details' heading
            employee_heading_style = ParagraphStyle(
                'EmployeeHeading',
                parent=styles['Heading2'],
                alignment=1
            )
            elements.append(Paragraph("Employee Details", employee_heading_style))
            
            employee_data = [['Employee', 'Basic', 'Allowances', 'Gross', 'Deductions', 'Net']]
            for record in records:
                employee = Employee.query.get(record.employee_id)
                if employee:
                    employee_data.append([
                        employee.name,
                        f"K {record.basic_salary:,.2f}",
                        f"K {record.housing_allowance + record.lunch_allowance + record.transport_allowance:,.2f}",
                        f"K {record.gross_pay:,.2f}",
                        f"K {record.total_deductions:,.2f}",
                        f"K {record.net_pay:,.2f}"
                    ])
            
            employee_table = Table(employee_data, colWidths=[150, 100, 100, 100, 100, 100])
            employee_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 8),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('GRID', (0, 0), (-1, -1), 1, colors.black)
            ]))
            elements.append(employee_table)
            
            try:
                # Build the PDF
                doc.build(elements)
                
                # Save report to database
                existing_report = Report.query.filter_by(name=report_name, year=year, month=month, type='monthly').first()
                if existing_report:
                    existing_report.generated_at = current_time
                    existing_report.file_path = file_path
                    existing_report.format = 'pdf'
                    existing_report.id = report_id
                else:
                    new_report = Report(
                        id=report_id,
                        name=report_name,
                        type='monthly',
                        format='pdf',
                        generated_at=current_time,
                        year=year,
                        month=month,
                        file_path=file_path
                    )
                    db.session.add(new_report)
                db.session.commit()
                
                # Add the newly generated report to the list
                reports.append({
                    'name': report_name,
                    'period': f"{get_month_name(month)} {year}",
                    'type': 'Monthly Summary',
                    'format': 'pdf',
                    'generated_at': current_time_str,
                    'id': report_id,
                    'total_employees': len(records),
                    'total_basic': total_basic,
                    'total_gross': total_gross_pay,
                    'total_net': total_net
                })
                
                flash('Monthly payroll report generated successfully.', 'success')
            except Exception as e:
                app.logger.error(f"Error generating PDF report: {str(e)}")
                flash('Error generating report. Please try again.', 'error')
                return redirect(url_for('payroll_reports'))
    
    # Add existing reports to the list
    for report in existing_reports:
        reports.append({
            'name': report.name,
            'period': f"{get_month_name(report.month)} {report.year}" if report.month else f"Year {report.year}",
            'type': 'Monthly Summary' if report.type == 'monthly' else 'Annual Summary',
            'format': report.format,
            'generated_at': report.generated_at.strftime('%Y-%m-%d %H:%M:%S'),
            'id': report.id
        })
    
    return render_template('payroll_reports.html',
                         reports=reports,
                         year=year,
                         month=month,
                         report_type=report_type,
                         report_format=report_format)

@app.route('/download_report_legacy')
@login_required
def download_report_legacy():
    report_id = request.args.get('id')
    
    # If report_id is provided, get the report from the database
    if report_id:
        report = Report.query.get_or_404(report_id)
        if report:
            # Check if the file exists in the static folder
            file_path = os.path.join(app.root_path, 'static', report.file_path.replace('/', os.path.sep))
            if os.path.exists(file_path):
                return send_file(file_path, as_attachment=True)
            else:
                # If file doesn't exist, regenerate it
                year = report.year
                month = report.month
                report_type = report.type
        else:
            flash('Report not found.', 'error')
            return redirect(url_for('payroll_reports'))
    else:
        # Legacy support for old URLs
        year = request.args.get('year', datetime.now().year, type=int)
        month = request.args.get('month', datetime.now().month, type=int)
        report_type = request.args.get('type', 'monthly')
    
    # Create a BytesIO buffer for the PDF
    buffer = BytesIO()
    
    # Create the PDF document with better styling
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(letter),
        rightMargin=5,
        leftMargin=5,
        topMargin=5,
        bottomMargin=5
    )
    elements = []
    styles = getSampleStyleSheet()
    
    # Add company information if available
    company = Company.query.first()
    if company:
        elements.append(Paragraph(company.name, styles['Heading1']))
        elements.append(Paragraph(company.address, styles['Normal']))
        elements.append(Paragraph(f"Phone: {company.phone}", styles['Normal']))
        elements.append(Paragraph(f"Email: {company.email}", styles['Normal']))
    
    elements.append(Spacer(1, 20))
    
    # Report title
    if report_type == 'monthly':
        title = f"Payroll Report - {get_month_name(month)} {year}"
    else:
        title = f"Annual Payroll Report - {year}"
    elements.append(Paragraph(title, styles['Heading1']))
    elements.append(Spacer(1, 20))
    
    # Get the same data as in the web view
    records = PayrollRecord.query.filter(
        extract('year', PayrollRecord.pay_date) == year
    )
    if report_type == 'monthly':
        records = records.filter(extract('month', PayrollRecord.pay_date) == month)
    records = records.all()
    
    # Get company information
    company = Company.query.first()
    
    # Calculate summary data
    if report_type == 'monthly':
        summary_data = {
            'total_basic_salary': sum(record.basic_salary for record in records),
            'total_housing_allowance': sum(record.housing_allowance for record in records),
            'total_lunch_allowance': sum(record.lunch_allowance for record in records),
            'total_transport_allowance': sum(record.transport_allowance for record in records),
            'total_overtime': sum(record.overtime for record in records),
            'total_gross_pay': sum(record.gross_pay for record in records),
            'total_paye': sum(record.paye for record in records),
            'total_napsa': sum(record.napsa for record in records),
            'total_nhima': sum(record.nhima for record in records),
            'total_deductions': sum(record.total_deductions for record in records),
            'total_net_pay': sum(record.net_pay for record in records)
        }
    else:
        # Group by month for annual report
        monthly_data = []
        for m in range(1, 13):
            month_records = [r for r in records if r.pay_date.month == m]
            if month_records:
                monthly_data.append({
                    'month': get_month_name(m),
                    'gross_pay': sum(r.gross_pay for r in month_records),
                    'deductions': sum(r.total_deductions for r in month_records),
                    'net_pay': sum(r.net_pay for r in month_records),
                    'employees': len(month_records)
                })
        summary_data = {'monthly_data': monthly_data}
    
    # Calculate payroll totals for current month
    total_monthly_payroll = sum(record.net_pay for record in records) if records else 0
    total_monthly_deductions = sum(record.total_deductions for record in records) if records else 0
    total_monthly_gross = sum(record.gross_pay for record in records) if records else 0
    total_paye = sum(record.paye for record in records) if records else 0
    
    total_napsa = sum(record.napsa for record in records) if records else 0
    total_nhima = sum(record.nhima for record in records) if records else 0
    
# Get tax brackets (simplified for now)
    paye_brackets = [
        {'min': 0, 'max': 4800, 'rate': 0},
        {'min': 4801, 'max': 6400, 'rate': 0.25},
        {'min': 6401, 'max': 8700, 'rate': 0.30},
        {'min': 8701, 'max': float('inf'), 'rate': 0.375}
    ]
    
    # Get department data for charts
    departments = db.session.query(Employee.department, func.count(Employee.id)).group_by(Employee.department).all()
    department_labels = [dept[0] if dept[0] else 'Unassigned' for dept in departments]
    department_counts = [dept[1] for dept in departments]
    
    # Get branch data for charts
    branches = db.session.query(Branch.name, func.count(Employee.id)).join(Employee, Employee.branch_id == Branch.id, isouter=True).group_by(Branch.name).all()
    branch_labels = [branch[0] if branch[0] else 'Unassigned' for branch in branches]
    branch_counts = [branch[1] for branch in branches]

    # Get recent reports
    recent_reports = Report.query.order_by(Report.generated_at.desc()).limit(5).all()

    return render_template('payroll_reports.html', reports=reports, current_year=year, current_month=month, report_type=report_type)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out successfully.', 'success')
    return redirect(url_for('login'))

@app.route('/reports')
@login_required
def reports():
    """Legacy route - redirect to payroll_reports"""
    return redirect(url_for('payroll_reports'))

@app.route('/view_report/<string:id>')
@login_required
def view_report(id):
    report = Report.query.get_or_404(id)
    # Redirect to the HTML view of the report instead of trying to display the PDF directly
    return redirect(url_for('view_report_html', id=report.id))

@app.route('/delete_report/<string:id>')
@login_required
def delete_report(id):
    report = Report.query.get_or_404(id)
    
    # Delete the file from the filesystem
    file_path = os.path.join(app.static_folder, report.file_path.replace('/', os.path.sep))
    if os.path.exists(file_path):
        os.remove(file_path)
    
    # Delete the database record
    db.session.delete(report)
    db.session.commit()
    
    flash('Report deleted successfully!', 'success')
    
    # Safer redirect logic
    if request.referrer:
        if '/payroll_reports' in request.referrer:
            return redirect(url_for('payroll_reports'))
        elif '/dashboard' in request.referrer:
            return redirect(url_for('dashboard'))
    
    # Default fallback
    return redirect(url_for('dashboard'))

@app.route('/download_report/<string:id>')
@login_required
def download_report(id):
    report = Report.query.get_or_404(id)
    file_path = os.path.join(app.static_folder, report.file_path.replace('/', os.path.sep))
    
    if not os.path.exists(file_path):
        app.logger.error(f"Report file not found at path: {file_path}")
        flash('Report file not found. Please regenerate the report.', 'error')
        return redirect(url_for('payroll_reports'))
    
    try:
        return send_file(
            file_path,
            as_attachment=True,
            download_name=f"{report.name}.pdf"
        )
    except Exception as e:
        app.logger.error(f"Error downloading report: {str(e)}")
        flash('Error downloading report. Please try again.', 'error')
        return redirect(url_for('payroll_reports'))

@app.route('/serve_report/<string:id>')
@login_required
def serve_report(id):
    report = Report.query.get_or_404(id)
    file_path = os.path.join(app.static_folder, report.file_path.replace('/', os.path.sep))
    
    if not os.path.exists(file_path):
        flash('Report file not found.', 'error')
        return redirect(url_for('payroll_reports'))
    
    return send_file(file_path)

@app.route('/view_pdf/<path:filename>')
@login_required
def view_pdf(filename):
    """Serve a PDF file for inline viewing in the browser."""
    try:
        # Construct the full path to the file within the static/reports directory
        file_path = os.path.join(app.static_folder, 'reports', filename)
        
        if not os.path.exists(file_path):
            flash('Report file not found.', 'error')
            return redirect(url_for('payroll_reports'))
        
        # Read the file content directly
        with open(file_path, 'rb') as f:
            binary_pdf = f.read()
        
        # Create a response with the PDF content
        response = make_response(binary_pdf)
        response.headers['Content-Type'] = 'application/pdf'
        # Force inline display with explicit headers
        response.headers['Content-Disposition'] = 'inline; filename="' + filename + '"'
        # Add cache control headers to prevent caching issues
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        # Add Cross-Origin headers to help with iframe display
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        
        return response
    except Exception as e:
        app.logger.error(f"Error serving PDF file: {str(e)}")
        flash('An error occurred while trying to display the report.', 'error')
        return redirect(url_for('payroll_reports'))




@app.route('/view_payslip/<int:record_id>')
@login_required
def view_payslip(record_id):
    """View a payslip in an HTML page with embedded PDF viewer"""
    record = PayrollRecord.query.get_or_404(record_id)
    company = Company.query.first()
    
    # Format the pay month - convert string date to datetime first
    try:
        # Try to parse the date string into a datetime object
        date_obj = datetime.strptime(record.pay_date, '%Y-%m-%d')
        pay_month = date_obj.strftime('%B %Y')
    except (ValueError, TypeError):
        # If parsing fails, use a generic format
        pay_month = record.pay_date
    
    # Generate the current date and time for the footer
    generated_date = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    return render_template('view_payslip.html', 
                           record=record,
                           company=company,
                           pay_month=pay_month,
                           generated_date=generated_date)
@app.route('/view_report_html/<string:id>')
@login_required
def view_report_html(id):
    """Render report data as HTML for viewing in the browser"""
    report = Report.query.get_or_404(id)
    
    # Get the report data based on the report type
    year = report.year
    month = report.month
    report_type = report.type
    
    # For verification reports, we need to get active employees rather than payroll records
    if report_type == 'verification':
        # Get active employees
        employees = Employee.query.filter_by(is_active=True).all()
        
        # Calculate totals
        total_basic = sum(e.basic_salary for e in employees)
        total_housing = sum(e.housing_allowance for e in employees)
        total_transport = sum(e.transport_allowance for e in employees)
        total_lunch = sum(e.lunch_allowance for e in employees)
        
        # Calculate total allowances for each employee
        for e in employees:
            e.total_allowances = e.housing_allowance + e.transport_allowance + e.lunch_allowance
            e.gross_pay = e.basic_salary + e.total_allowances
            
            # Calculate deductions
            e.napsa = min(e.basic_salary * 0.05, 1073.25)  # 5% of basic up to the cap
            
            # Calculate PAYE (tax brackets from Zambian tax system)
            taxable_income = e.gross_pay - e.napsa
            if taxable_income <= 4500:
                e.paye = 0
            elif taxable_income <= 4800:
                e.paye = (taxable_income - 4500) * 0.25
            elif taxable_income <= 6900:
                e.paye = 75 + (taxable_income - 4800) * 0.30
            else:
                e.paye = 705 + (taxable_income - 6900) * 0.375
            
            # NHIMA (National Health Insurance)
            e.nhima = e.basic_salary * 0.01  # 1% of basic salary
            
            # Total deductions and net pay
            e.total_deductions = (
                e.napsa + e.paye + e.nhima +
                getattr(e, 'salary_advance', 0) +
                getattr(e, 'rainbow_loan', 0) +
                getattr(e, 'other_deduction', 0)
            )
            e.net_pay = e.gross_pay - e.total_deductions
        
        # Calculate summary data
        summary_data = {
            'total_basic_salary': total_basic,
            'total_housing_allowance': total_housing,
            'total_lunch_allowance': total_lunch,
            'total_transport_allowance': total_transport,
            'total_allowances': total_housing + total_lunch + total_transport,
            'total_gross_pay': total_basic + total_housing + total_lunch + total_transport,
            'total_napsa': sum(min(e.basic_salary * 0.05, 1073.25) for e in employees),
            'total_paye': sum(e.paye for e in employees),
            'total_nhima': sum(e.nhima for e in employees),
            'total_salary_advance': sum(getattr(e, 'salary_advance', 0) for e in employees),
            'total_rainbow_loan': sum(getattr(e, 'rainbow_loan', 0) for e in employees),
            'total_other_deduction': sum(getattr(e, 'other_deduction', 0) for e in employees),
            'total_deductions': sum(e.total_deductions for e in employees),
            'total_net_pay': sum(e.net_pay for e in employees)
        }
        
        # Render the HTML template with the report data
        return render_template(
            'report_html.html',
            report=report,
            company=Company.query.first(),
            employees=employees,
            summary_data=summary_data,
            report_type=report_type
        )
    
    # For regular payroll reports (not verification reports)
    else:
        # Get the payroll records for the report period
        records = PayrollRecord.query.filter(
            extract('year', PayrollRecord.pay_date) == year
        )
        
        if report_type == 'monthly':
            records = records.filter(extract('month', PayrollRecord.pay_date) == month)
        
        records = records.all()
        
        # Get company information
        company = Company.query.first()
        
        # Calculate summary data
        if report_type == 'monthly':
            summary_data = {
                'total_basic_salary': sum(record.basic_salary for record in records),
                'total_housing_allowance': sum(record.housing_allowance for record in records),
                'total_lunch_allowance': sum(record.lunch_allowance for record in records),
                'total_transport_allowance': sum(record.transport_allowance for record in records),
                'total_overtime': sum(record.overtime for record in records),
                'total_gross_pay': sum(record.gross_pay for record in records),
                'total_paye': sum(record.paye for record in records),
                'total_napsa': sum(record.napsa for record in records),
                'total_nhima': sum(record.nhima for record in records),
                'total_deductions': sum(record.total_deductions for record in records),
                'total_net_pay': sum(record.net_pay for record in records)
            }
        else:
            # Group by month for annual report
            monthly_data = []
            for m in range(1, 13):
                month_records = [r for r in records if r.pay_date.month == m]
                if month_records:
                    monthly_data.append({
                        'month': get_month_name(m),
                        'gross_pay': sum(r.gross_pay for r in month_records),
                        'deductions': sum(r.total_deductions for r in month_records),
                        'net_pay': sum(r.net_pay for r in month_records),
                        'employees': len(month_records)
                    })
            summary_data = {'monthly_data': monthly_data}
        
        # Render the HTML template with the report data
        return render_template(
            'report_html.html',
            report=report,
            company=company,
            records=records,
            summary_data=summary_data,
            report_type=report_type
        )


@app.route('/test')
def test_route():
    return "Server is working!"

@app.route('/api/get_department_data')
@login_required
def get_department_data():
    # Department distribution data
    employees = Employee.query.all()
    department_distribution = {}
    for employee in employees:
        if employee.department in department_distribution:
            department_distribution[employee.department] += 1
        else:
            department_distribution[employee.department] = 1
    
    # Prepare data for chart
    labels = list(department_distribution.keys())
    counts = [department_distribution[dept] for dept in labels]
    
    return jsonify({
        'labels': labels,
        'counts': counts
    })

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if not current_user.is_admin:
        flash('You do not have permission to access this page.', 'danger')
        return redirect(url_for('dashboard'))
    
    # Create backups directory if it doesn't exist
    backup_dir = os.path.join(app.root_path, 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    
    # Handle database backup
    if request.method == 'POST' and 'backup_db' in request.form:
        try:
            # Create timestamp for backup filename
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_filename = f'payroll_backup_{timestamp}.db'
            backup_path = os.path.join(backup_dir, backup_filename)
            
            # Get the database connection string and create a backup
            db_path = os.path.join(app.root_path, 'instance', 'payroll.db')
            
            # Ensure all changes are committed before backup
            db.session.commit()
            
            # Close connections to allow proper file access
            db.session.close()
            db.engine.dispose()
            
            # Create a backup using SQLite's backup API
            import sqlite3
            
            # Connect to the source database
            source_conn = sqlite3.connect(db_path)
            
            # Connect to the destination database (backup file)
            dest_conn = sqlite3.connect(backup_path)
            
            # Backup the database
            source_conn.backup(dest_conn)
            
            # Close connections
            source_conn.close()
            dest_conn.close()
            
            # Reconnect to the database
            db.create_all()
            
            flash(f'Database backup created successfully: {backup_filename}', 'success')
        except Exception as e:
            flash(f'Error creating backup: {str(e)}', 'danger')
        
        return redirect(url_for('settings'))
    
    # Handle database restore
    if request.method == 'POST' and 'restore_db' in request.form:
        backup_file = request.form.get('backup_file')
        if backup_file:
            try:
                backup_path = os.path.join(backup_dir, backup_file)
                db_path = os.path.join(app.root_path, 'instance', 'payroll.db')
                
                # Close all database connections
                db.session.remove()
                db.engine.dispose()
                
                # Use direct file copy instead of SQLite backup API
                import shutil
                import time
                
                # Make a backup of the current database before overwriting
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                current_backup = os.path.join(backup_dir, f'pre_restore_backup_{timestamp}.db')
                shutil.copy2(db_path, current_backup)
                
                # Wait a moment to ensure file operations are complete
                time.sleep(1)
                
                # Copy the backup file to the database location
                try:
                    # First try to copy directly
                    shutil.copy2(backup_path, db_path)
                except PermissionError:
                    # If permission error, try to force close any remaining connections
                    import gc
                    gc.collect()  # Force garbage collection to release any file handles
                    time.sleep(1)
                    # Try again
                    shutil.copy2(backup_path, db_path)
                
                # Create a special flag file with the path to the restored database
                restore_info_file = os.path.join(app.root_path, 'restore_info.json')
                with open(restore_info_file, 'w') as f:
                    import json
                    json.dump({
                        'timestamp': str(datetime.now()),
                        'backup_file': backup_file,
                        'backup_path': backup_path,
                        'db_path': db_path
                    }, f)
                
                # Create a restart flag to ensure application restart
                restart_flag_file = os.path.join(app.root_path, 'restart.flag')
                with open(restart_flag_file, 'w') as f:
                    f.write(str(datetime.now()))
                
                flash('Database restored successfully. The application will now restart.', 'success')
                return redirect(url_for('restart_status'))
            except Exception as e:
                flash(f'Error restoring database: {str(e)}', 'danger')
        else:
            flash('No backup file selected', 'warning')
        
        return redirect(url_for('settings'))
    
    # Get list of backup files
    backup_files = []
    try:
        backup_files = [f for f in os.listdir(backup_dir) if f.endswith('.db')]
        backup_files.sort(reverse=True)  # Most recent first
    except Exception as e:
        flash(f'Error reading backup directory: {str(e)}', 'danger')
    
    return render_template('settings.html', backup_files=backup_files)

@app.route('/download-backup/<filename>')
@login_required
def download_backup(filename):
    if not current_user.is_admin:
        flash('You do not have permission to access this page.', 'danger')
        return redirect(url_for('dashboard'))
    
    backup_dir = os.path.join(app.root_path, 'backups')
    return send_from_directory(backup_dir, filename, as_attachment=True)

@app.route('/delete-backup', methods=['POST'])
@login_required
def delete_backup():
    if not current_user.is_admin:
        flash('You do not have permission to access this page.', 'danger')
        return redirect(url_for('dashboard'))
    
    if 'delete_backup' in request.form and 'backup_file' in request.form:
        backup_file = request.form.get('backup_file')
        if backup_file:
            try:
                backup_path = os.path.join(app.root_path, 'backups', backup_file)
                if os.path.exists(backup_path):
                    os.remove(backup_path)
                    flash(f'Backup file {backup_file} deleted successfully', 'success')
                else:
                    flash(f'Backup file {backup_file} not found', 'warning')
            except Exception as e:
                flash(f'Error deleting backup: {str(e)}', 'danger')
    
    return redirect(url_for('settings'))

@app.route('/import-employees', methods=['GET', 'POST'])
@login_required
def import_employees():
    if request.method == 'POST':
        # Check if the post request has the file part
        if 'excel_file' not in request.files:
            flash('No file part', 'danger')
            return redirect(request.url)
        
        file = request.files['excel_file']
        
        if file.filename == '':
            flash('No selected file', 'danger')
            return redirect(request.url)
        
        # Check if the file is an Excel file
        if not file.filename.endswith(('.xlsx', '.xls')):
            flash('File must be an Excel file (.xlsx or .xls)', 'danger')
            return redirect(request.url)
        
        # Process the Excel file
        try:
            import pandas as pd
            import random
            import string
            
            # Read the Excel file
            df = pd.read_excel(file)
            
            # Validate required columns
            required_columns = ['Employee Name', 'Position', 'Basic Salary']
            missing_columns = [col for col in required_columns if col not in df.columns]
            
            if missing_columns:
                flash(f'Missing required columns: {", ".join(missing_columns)}', 'danger')
                return redirect(request.url)
            
            # Track import results
            success_count = 0
            error_count = 0
            error_messages = []
            
            # Process each row
            for index, row in df.iterrows():
                try:
                    # Check for duplicate NRC (skip or flag if exists and not blank)
                    nrc_value = '' if pd.isna(row.get('NRC')) else str(row.get('NRC', ''))
                    if nrc_value:
                        existing_employee = Employee.query.filter_by(nrc=nrc_value).first()
                        if existing_employee:
                            error_count += 1
                            error_messages.append(f"Row {index+1}: Duplicate NRC '{nrc_value}' found. Employee skipped.")
                            continue  # Skip this row
                    
                    # Generate a unique employee code
                    employee_code = 'EMP' + ''.join(random.choices(string.digits, k=5))
                    while Employee.query.filter_by(employee_code=employee_code).first():
                        employee_code = 'EMP' + ''.join(random.choices(string.digits, k=5))
                    
                    # Create a new employee
                    employee = Employee(
                        employee_code=employee_code,
                        name=row['Employee Name'],
                        nrc=nrc_value,
                        email='' if pd.isna(row.get('Email')) else str(row.get('Email', '')),
                        position=row['Position'],
                        department='' if pd.isna(row.get('Department')) else str(row.get('Department', '')),
                        basic_salary=float(row['Basic Salary']),
                        housing_allowance=float(0 if pd.isna(row.get('Housing Allowance')) else row.get('Housing Allowance', 0)),
                        lunch_allowance=float(0 if pd.isna(row.get('Lunch Allowance')) else row.get('Lunch Allowance', 0)),
                        transport_allowance=float(0 if pd.isna(row.get('Transport Allowance')) else row.get('Transport Allowance', 0)),
                        overtime=float(0 if pd.isna(row.get('Overtime')) else row.get('Overtime', 0)),
                        salary_advance=float(0 if pd.isna(row.get('Salary Advance')) else row.get('Salary Advance', 0)),
                        rainbow_loan=float(0 if pd.isna(row.get('Rainbow Loan')) else row.get('Rainbow Loan', 0)),
                        other_deduction=float(0 if pd.isna(row.get('Other Deduction')) else row.get('Other Deduction', 0)),
                        other_deduction_reason='' if pd.isna(row.get('Other Deduction Reason')) else str(row.get('Other Deduction Reason', '')),
                        branch_id=1  # Assuming there's at least one branch with ID 1
                    )
                    
                    db.session.add(employee)
                    success_count += 1
                except Exception as e:
                    error_count += 1
                    error_messages.append(f"Row {index+1}: {str(e)}")
            
            # Commit changes if there were any successful imports
            if success_count > 0:
                db.session.commit()
            
            # Show import results
            if success_count > 0:
                flash(f'Successfully imported {success_count} employees', 'success')
            
            if error_count > 0:
                flash(f'Failed to import {error_count} employees', 'danger')
                for msg in error_messages[:10]:  # Show first 10 errors
                    flash(msg, 'warning')
                if len(error_messages) > 10:
                    flash(f'... and {len(error_messages) - 10} more errors', 'warning')
            
            return redirect(url_for('employees'))
            
        except Exception as e:
            flash(f'Error processing file: {str(e)}', 'danger')
            return redirect(request.url)
    
    return render_template('import_employees.html')

@app.route('/download_employee_template')
@login_required
def download_employee_template():
    import pandas as pd
    from io import BytesIO
    
    # Create a sample DataFrame
    data = {
        'Employee Name': ['John Doe', 'Jane Smith'],
        'Position': ['Manager', 'Accountant'],
        'Department': ['Administration', 'Finance'],  # Now optional
        'Basic Salary': [5000, 3500],
        'Housing Allowance': [1000, 800],
        'Lunch Allowance': [500, 300],
        'Transport Allowance': [300, 200],
        'Overtime': [0, 0],
        'Salary Advance': [0, 0],
        'Rainbow Loan': [0, 0],
        'Other Deduction': [0, 0],
        'Other Deduction Reason': ['Reason for Other Deduction', ''],  # New field
        'NRC': ['123456/78/9', ''],  # Now optional - second row empty to show it's optional
    }
    
    df = pd.DataFrame(data)
    
    # Create an Excel file in memory
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Employees')
    
    output.seek(0)
    
    return send_file(
        output,
        as_attachment=True,
        download_name='employee_import_template.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route('/delete_multiple_employees', methods=['POST'])
@login_required
def delete_multiple_employees():
    employee_ids = request.form.getlist('employee_ids')
    
    if not employee_ids:
        flash('No employees selected for deletion.', 'warning')
        return redirect(url_for('employees'))
    
    try:
        # Get count for success message
        count = len(employee_ids)
        
        # Delete each employee
        for employee_id in employee_ids:
            employee = Employee.query.get(employee_id)
            if employee:
                db.session.delete(employee)
        
        db.session.commit()
        flash(f'{count} employees deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting employees: {str(e)}', 'error')
    
    return redirect(url_for('employees'))

@app.route('/delete_multiple_reports', methods=['POST'])
@login_required
def delete_multiple_reports():
    report_ids = request.form.getlist('report_ids')
    
    if not report_ids:
        flash('No reports selected for deletion.', 'warning')
        return redirect(url_for('payroll_reports'))

    try:
        # Get count for success message
        count = len(report_ids)
        deleted_count = 0
        
        # Delete each report
        for report_id in report_ids:
            report = Report.query.get(report_id)
            if report:
                # Delete the file from the filesystem
                file_path = os.path.join(app.static_folder, report.file_path.replace('/', os.path.sep))
                if os.path.exists(file_path):
                    os.remove(file_path)
                
                # Delete the database record
                db.session.delete(report)
                deleted_count += 1
        
        db.session.commit()
        flash(f'{deleted_count} reports deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting reports: {str(e)}', 'error')
    
    return redirect(url_for('payroll_reports'))

@app.route('/restart-app', methods=['GET', 'POST'])
@login_required
def restart_app():
    """Restart the Flask application"""
    if not current_user.is_admin:
        flash('You do not have permission to restart the application.', 'danger')
        return redirect(url_for('dashboard'))
    
    # Show a confirmation page if this is a GET request
    if request.method == 'GET':
        return render_template('restart.html')
    
    # For POST requests, create a restart flag file and redirect to a page that checks for it
    try:
        restart_flag_file = os.path.join(app.root_path, 'restart.flag')
        with open(restart_flag_file, 'w') as f:
            f.write(str(datetime.now()))
        
        flash('Application restart initiated. Please refresh your browser in a few seconds.', 'info')
        return redirect(url_for('restart_status'))
    except Exception as e:
        flash(f'Error initiating restart: {str(e)}', 'danger')
        return redirect(url_for('settings'))

@app.route('/restart-status')
def restart_status():
    """Show restart status page"""
    return render_template('restart_status.html')

# Check for restart flag on application startup
restart_flag_file = os.path.join(app.root_path, 'restart.flag')
if os.path.exists(restart_flag_file):
    try:
        # Remove the flag file
        os.remove(restart_flag_file)
        print("Restart flag detected and removed. Reinitializing database connection.")
        
        # Force reconnection to the database
        db.session.remove()
        db.engine.dispose()
        
        # Wait a moment to ensure file operations are complete
        import time
        time.sleep(1)
        
        # Reconnect to the database with a fresh engine
        from sqlalchemy import create_engine
        from sqlalchemy.orm import scoped_session, sessionmaker
        
        db_uri = f"sqlite:///{os.path.join(app.root_path, 'instance', 'payroll.db')}"
        new_engine = create_engine(db_uri)
        db.engine = new_engine
        db.session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=new_engine))
        
        # Ensure all models are properly initialized
        db.Model.metadata.clear()
        db.create_all()
        
        print("Database connection reinitialized successfully.")
    except Exception as e:
        print(f"Error during database reinitialization: {str(e)}")

# Check for restore info file on application startup
restore_info_file = os.path.join(app.root_path, 'restore_info.json')
if os.path.exists(restore_info_file):
    try:
        import json
        # Read the restore info
        with open(restore_info_file, 'r') as f:
            restore_info = json.load(f)
            
        # Remove the info file to prevent reprocessing
        os.remove(restore_info_file)
        print(f"Database restore detected. Restored from: {restore_info.get('backup_file')}")
        
        # Get the database path
        db_path = restore_info.get('db_path')
        backup_path = restore_info.get('backup_path')
        
        # Verify the backup file exists
        if not os.path.exists(backup_path):
            print(f"Error: Backup file not found at {backup_path}")
            raise FileNotFoundError(f"Backup file not found at {backup_path}")
            
        # Copy the backup file to the database location
        import shutil
        import time
        
        # Force close all database connections
        db.session.close()
        db.engine.dispose()
        
        # Wait to ensure connections are closed
        time.sleep(1)
        
        # Reconnect to the database with a fresh engine
        from sqlalchemy import create_engine
        from sqlalchemy.orm import scoped_session, sessionmaker
        
        db_uri = f"sqlite:///{os.path.join(app.root_path, 'instance', 'payroll.db')}"
        new_engine = create_engine(db_uri)
        db.engine = new_engine
        db.session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=new_engine))
        
        # Ensure all models are properly initialized
        db.Model.metadata.clear()
        db.create_all()
        
        print("Database connection reinitialized successfully.")
        
        # Copy the backup file to the database location
        try:
            # Remove the existing database file first
            if os.path.exists(db_path):
                os.remove(db_path)
                time.sleep(0.5)  # Brief pause
                
            # Copy the backup file to the database location
            shutil.copy2(backup_path, db_path)
            time.sleep(0.5)  # Brief pause
            
            print(f"Successfully copied backup file to database location: {db_path}")
        except Exception as e:
            print(f"Error copying backup file: {str(e)}")
            raise
        
        # Create a restart flag to ensure application restart
        restart_flag_file = os.path.join(app.root_path, 'restart.flag')
        with open(restart_flag_file, 'w') as f:
            f.write(str(datetime.now()))
        
        print("Restart flag created. Please refresh your browser in a few seconds.")
    except Exception as e:
        print(f"Error during database restore initialization: {str(e)}")

@app.route('/edit_user/<int:user_id>', methods=['POST'])
@login_required
def edit_user(user_id):
    # Only admins can edit users
    if not current_user.is_admin:
        flash('Permission denied', 'danger')
        return redirect(url_for('profile'))
    
    user = User.query.get_or_404(user_id)
    
    # Don't allow changing your own admin status (to prevent lockout)
    if user.id == current_user.id and 'is_admin' not in request.form:
        flash('You cannot remove your own admin privileges', 'danger')
        return redirect(url_for('profile'))
    
    # Update user details
    user.username = request.form.get('edit_username')
    
    # Update password if provided
    new_password = request.form.get('edit_password')
    if new_password and new_password.strip():
        user.password = generate_password_hash(new_password)
    
    # Update admin status
    user.is_admin = 'edit_is_admin' in request.form
    
    db.session.commit()
    flash(f'User {user.username} updated successfully', 'success')
    return redirect(url_for('profile'))

@app.route('/delete_user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    # Only admins can delete users
    if not current_user.is_admin:
        flash('Permission denied', 'danger')
        return redirect(url_for('profile'))
    
    user = User.query.get_or_404(user_id)
    
    # Prevent self-deletion
    if user.id == current_user.id:
        flash('You cannot delete your own account', 'danger')
        return redirect(url_for('profile'))
    
    username = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f'User {username} deleted successfully', 'success')
    return redirect(url_for('profile'))

@app.route('/send_payslip_email/<int:payroll_id>', methods=['POST'])
@login_required
def send_payslip_email_route(payroll_id):
    """Send a payslip via email"""
    if not current_user.is_admin:
        flash('Permission denied', 'danger')
        return redirect(url_for('payroll_records'))
    
    payroll_record = PayrollRecord.query.get_or_404(payroll_id)
    employee_id = payroll_record.employee_id
    
    custom_email = request.form.get('custom_email')
    success, message = send_payslip_email(employee_id, payroll_id, custom_email)
    
    if success:
        flash('Payslip email sent successfully', 'success')
    else:
        flash(f'Error sending payslip email: {message}', 'danger')
    
    return redirect(url_for('payroll_records'))

@app.route('/send_custom_email/<int:record_id>', methods=['POST'])
@login_required
def send_custom_email_route(record_id):
    record = PayrollRecord.query.get_or_404(record_id)
    
    # Get the custom email from the form
    custom_email = request.form.get('custom_email')
    
    # Validate custom email
    if not custom_email:
        flash('Please provide a valid email address.', 'error')
        return redirect(url_for('payroll_records'))
    
    # Get company information for email settings
    company = Company.query.first()
    # Check if company exists
    if not company:
        flash('Company information is not configured.', 'error')
        return redirect(url_for('payroll_records'))

    # Import the email utility function
    from fixed_email_utils import send_smtp_email

    # Get email settings from database
    # Using EmailSettings model defined in this file
    email_settings = EmailSettings.query.first()

    if not email_settings:
        flash('Email settings are not configured.', 'error')
        return redirect(url_for('payroll_records'))
    try:
        # Generate PDF for the payslip
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        styles = getSampleStyleSheet()
        
        # Create the content
        content = []
        
        # Title
        title_style = styles['Heading1']
        content.append(Paragraph(f"{company.name} - Payslip", title_style))
        content.append(Spacer(1, 12))
        
        # Employee and payroll information
        info_style = styles['Normal']
        content.append(Paragraph(f"<b>Employee:</b> {record.employee.name}", info_style))
        content.append(Paragraph(f"<b>Employee Code:</b> {record.employee.employee_code}", info_style))
        content.append(Paragraph(f"<b>Pay Date:</b> {record.pay_date}", info_style))
        # Pay period derived from pay date
        pay_month = datetime.strptime(record.pay_date.split("-")[1], "%m").strftime("%B")
        pay_year = record.pay_date.split("-")[0]
        content.append(Paragraph(f"<b>Pay Period:</b> {pay_month} {pay_year}", info_style))
        content.append(Spacer(1, 12))
        
        # Earnings table
        data = [
            ['Earnings', 'Amount'],
            ['Basic Salary', f"K {record.basic_salary:.2f}"],
            ['Housing Allowance', f"K {record.housing_allowance:.2f}"],
            ['Transport Allowance', f"K {record.transport_allowance:.2f}"],
            ['Lunch Allowance', f"K {record.lunch_allowance:.2f}"],
            ['Overtime', f"K {record.overtime:.2f}"],
            ['Gross Pay', f"K {record.gross_pay:.2f}"]
        ]
        
        t = Table(data, colWidths=[300, 100])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (1, 0), 'CENTER'),
            ('FONTNAME', (0, 0), (1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (1, 0), 12),
            ('BACKGROUND', (0, -1), (1, -1), colors.lightgrey),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        
        content.append(t)
        content.append(Spacer(1, 12))
        
        # Deductions table
        data = [
            ['Deductions', 'Amount'],
            ['PAYE', f"K {record.paye:.2f}"],
            ['NAPSA', f"K {record.napsa:.2f}"],
            ['NHIMA', f"K {record.nhima:.2f}"],
            ['Salary Advance', f"K {record.salary_advance:.2f}"],
            ['Rainbow Loan', f"K {record.rainbow_loan:.2f}"],
            ['Other Deduction', f"K {record.other_deduction:.2f}"],
            ['Total Deductions', f"K {record.total_deductions:.2f}"]
        ]
        
        t = Table(data, colWidths=[300, 100])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (1, 0), 'CENTER'),
            ('FONTNAME', (0, 0), (1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (1, 0), 12),
            ('BACKGROUND', (0, -1), (1, -1), colors.lightgrey),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        
        content.append(t)
        content.append(Spacer(1, 12))
        
        # Net Pay
        content.append(Paragraph(f"<b>Net Pay:</b> K {record.net_pay:.2f}", ParagraphStyle('NetPay', fontSize=14, fontName='Helvetica-Bold')))
        
        # Build the PDF
        doc.build(content)
        pdf_data = buffer.getvalue()
        buffer.close()
        
        # Send email
        month_name = datetime.strptime(record.pay_date.split('-')[1], '%m').strftime('%B')
        year = record.pay_date.split('-')[0]
        subject = f"Payslip for {month_name} {year}"
        
        # Create email body
        body = f"""<html>
<body>
<p>Dear {record.employee.name},</p>
<p>Please find attached your payslip for {month_name} {year}.</p>
<p>Regards,<br>{company.name}</p>
</body>
</html>"""
        
        if company.email_signature:
            body = body.replace("</body>", f"<div>{company.email_signature}</div></body>")
        
        # Create attachment
        attachment_name = f"Payslip_{record.employee.employee_code}_{month_name}.pdf"
        attachments = [(attachment_name, pdf_data)]
        
        # Send email using the utility function
        success, message = send_email(
            subject=subject,
            recipients=[custom_email],  # Use the custom email directly
            html_body=body,
            attachments=attachments
        )
        
        if success:
            flash(f'Payslip sent to {custom_email} successfully!', 'success')
        else:
            flash(f'Error sending email: {message}', 'error')
    except Exception as e:
        flash(f'Error sending email: {str(e)}', 'error')
    
    return redirect(url_for('payroll_records'))


@app.route('/send_all_payslips/<int:year>/<int:month>', methods=['POST'])
@login_required
def send_all_payslips(year, month):
    # Get all payroll records for the specified month and year
    records = PayrollRecord.query.filter(
        extract('year', PayrollRecord.pay_date) == year,
        extract('month', PayrollRecord.pay_date) == month
    ).all()
    
    if not records:
        flash('No payroll records found for the specified period.', 'warning')
        return redirect(url_for('payroll_records'))
    
    success_count = 0
    error_count = 0
    
    for record in records:
        success, message = send_payslip_email(record.employee_id, record.id)
        if success:
            success_count += 1
        else:
            error_count += 1
    
    if success_count > 0:
        flash(f'Successfully sent {success_count} payslips.', 'success')
    if error_count > 0:
        flash(f'Failed to send {error_count} payslips.', 'warning')
    
    return redirect(url_for('payroll_records'))

@app.route('/export-employees')
@login_required
def export_employees():
    import pandas as pd
    from io import BytesIO
    
    # Get all employees
    employees = Employee.query.all()
    
    # Prepare data for export
    data = []
    for employee in employees:
        emp_data = {
            'Employee Code': employee.employee_code,
            'Employee Name': employee.name,
            'NRC': employee.nrc or '',
            'Email': employee.email or '',
            'Position': employee.position,
            'Department': employee.department or '',
            'Branch': employee.branch.name if employee.branch else '',
            'Basic Salary': employee.basic_salary,
            'Housing Allowance': employee.housing_allowance,
            'Lunch Allowance': employee.lunch_allowance,
            'Transport Allowance': employee.transport_allowance,
            'Overtime': employee.overtime,
            'Salary Advance': employee.salary_advance,
            'Rainbow Loan': employee.rainbow_loan,
            'Other Deduction': employee.other_deduction,
            'Other Deduction Reason': employee.other_deduction_reason or '',
            'Status': 'Active' if employee.is_active else 'Inactive',
            'Hire Date': employee.hire_date.strftime('%Y-%m-%d') if employee.hire_date else ''
        }
        data.append(emp_data)
    
    # Create DataFrame
    df = pd.DataFrame(data)
    
    # Create Excel file in memory
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Employees')
        
        # Auto-adjust columns' width
        worksheet = writer.sheets['Employees']
        for i, col in enumerate(df.columns):
            column_width = max(df[col].astype(str).map(len).max(), len(col)) + 2
            worksheet.column_dimensions[chr(65 + i)].width = column_width
    
    output.seek(0)
    
    # Generate timestamp for filename
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    return send_file(
        output,
        as_attachment=True,
        download_name=f'employees_export_{timestamp}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user)
            user.last_login = datetime.now()
            db.session.commit()
            
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            return redirect(url_for('dashboard'))
        
        flash('Invalid username or password', 'danger')
    
    return render_template('login.html')


# Safe version of Employee.query.count() that avoids missing columns
def safe_employee_count():
    try:
        result = db.session.execute(text("SELECT COUNT(*) FROM employee"))
        return result.scalar()
    except Exception as e:
        print(f"Error counting employees: {str(e)}")
        return 0

# Safe version of Employee.query.filter_by(is_active=True).count()
def safe_active_employee_count():
    try:
        result = db.session.execute(text("SELECT COUNT(*) FROM employee WHERE is_active = 1"))
        return result.scalar()
    except Exception as e:
        print(f"Error counting active employees: {str(e)}")
        return 0

# Safe version of Employee.query.filter_by(is_active=False).count()
def safe_inactive_employee_count():
    try:
        result = db.session.execute(text("SELECT COUNT(*) FROM employee WHERE is_active = 0"))
        return result.scalar()
    except Exception as e:
        print(f"Error counting inactive employees: {str(e)}")
        return 0



# Safe functions to query employees without relying on ORM columns
def get_employees_safely():
    try:
        # Use raw SQL to get only the columns we know exist
        sql_query = text('''
            SELECT id, employee_code, name, nrc, email, position, department, branch_id,
                   basic_salary, housing_allowance, lunch_allowance, transport_allowance, overtime,
                   hire_date, is_active
            FROM employee
        ''')
        
        result = db.session.execute(sql_query)
        
        # Convert result to a list of dictionaries
        employees = []
        for row in result:
            employee = {}
            for idx, column in enumerate(result.keys()):
                employee[column] = row[idx]
            # Add default values for missing columns
            employee['salary_advance'] = 0.0
            employee['rainbow_loan'] = 0.0
            employee['other_deduction'] = 0.0
            employee['other_deduction_reason'] = ''
            employees.append(employee)
        
        return employees
    except Exception as e:
        print(f"Error fetching employees: {str(e)}")
        return []

def get_active_employees_safely():
    try:
        # Use raw SQL to get only active employees with columns we know exist
        sql_query = text('''
            SELECT id, employee_code, name, nrc, email, position, department, branch_id,
                   basic_salary, housing_allowance, lunch_allowance, transport_allowance, overtime,
                   hire_date, is_active
            FROM employee
            WHERE is_active = 1
        ''')
        
        result = db.session.execute(sql_query)
        
        # Convert result to a list of dictionaries
        employees = []
        for row in result:
            employee = {}
            for idx, column in enumerate(result.keys()):
                employee[column] = row[idx]
            # Add default values for missing columns
            employee['salary_advance'] = 0.0
            employee['rainbow_loan'] = 0.0
            employee['other_deduction'] = 0.0
            employee['other_deduction_reason'] = ''
            employees.append(employee)
        
        return employees
    except Exception as e:
        print(f"Error fetching active employees: {str(e)}")
        return []


@app.route('/dashboard')
@login_required
def dashboard():
    # Get current month and year
    now = datetime.now()
    current_month = now.month
    current_year = now.year
    
    # Get company and license information
    company = Company.query.first()
    current_license = company.current_license if company else None
    license_status = "Active" if company and company.is_licensed else "Inactive"
    license_expiry = current_license.end_date.strftime("%Y-%m-%d") if current_license else "N/A"
    
    # Get total employees
    total_employees = safe_employee_count()
    
    # Get total active employees
    active_employees = safe_active_employee_count()
    
    # Get inactive employees
    inactive_employees = safe_inactive_employee_count()
    
    # Instead of querying PayrollRecord directly, use a safer approach
    # that doesn't rely on the missing columns
    try:
        # Use raw SQL to get only the columns we know exist
        sql_query = text("""
            SELECT id, employee_id, pay_date, basic_salary, housing_allowance, 
                   lunch_allowance, transport_allowance, overtime, gross_pay, 
                   paye, napsa, nhima, total_deductions, net_pay
            FROM payroll_record
            WHERE CAST(STRFTIME('%Y', pay_date) AS INTEGER) = :year
            AND CAST(STRFTIME('%m', pay_date) AS INTEGER) = :month
        """)
        
        result = db.session.execute(sql_query, {"year": current_year, "month": current_month})
        
        # Convert result to a list of dictionaries
        payroll_records = []
        for row in result:
            record = {}
            for idx, column in enumerate(result.keys()):
                record[column] = row[idx]
            payroll_records.append(record)
    except Exception as e:
        print(f"Error fetching payroll records: {str(e)}")
        payroll_records = []
    
    # Calculate payroll totals for current month
    # Use get() method to safely access dictionary keys
    total_monthly_payroll = sum(record.get('net_pay', 0) for record in payroll_records) if payroll_records else 0
    total_monthly_deductions = sum(record.get('total_deductions', 0) for record in payroll_records) if payroll_records else 0
    total_monthly_gross = sum(record.get('gross_pay', 0) for record in payroll_records) if payroll_records else 0
    total_paye = sum(record.get('paye', 0) for record in payroll_records) if payroll_records else 0
    
    total_napsa = sum(record.get('napsa', 0) for record in payroll_records) if payroll_records else 0
    total_nhima = sum(record.get('nhima', 0) for record in payroll_records) if payroll_records else 0
    
    # Get tax brackets (simplified for now)
    paye_brackets = [
        {'min': 0, 'max': 4800, 'rate': 0},
        {'min': 4801, 'max': 6400, 'rate': 0.25},
        {'min': 6401, 'max': 8700, 'rate': 0.30},
        {'min': 8701, 'max': float('inf'), 'rate': 0.375}
    ]
    
    # Get department data for charts
    departments = db.session.query(Employee.department, func.count(Employee.id)).group_by(Employee.department).all()
    department_labels = [dept[0] if dept[0] else 'Unassigned' for dept in departments]
    department_counts = [dept[1] for dept in departments]
    
    # Get branch data for charts
    branches = db.session.query(Branch.name, func.count(Employee.id)).join(Employee, Employee.branch_id == Branch.id, isouter=True).group_by(Branch.name).all()
    branch_labels = [branch[0] if branch[0] else 'Unassigned' for branch in branches]
    branch_counts = [branch[1] for branch in branches]

    # Get recent reports
    recent_reports = Report.query.order_by(Report.generated_at.desc()).limit(5).all()

    return render_template('dashboard.html', 
                          total_employees=total_employees,
                          active_employees=active_employees,
                          inactive_employees=inactive_employees,
                          total_payroll=total_monthly_payroll,  # For backward compatibility
                          total_monthly_payroll=total_monthly_payroll,
                          total_monthly_deductions=total_monthly_deductions,
                          total_monthly_gross=total_monthly_gross,
                          total_paye=total_paye,
                          total_napsa=total_napsa,
                          total_nhima=total_nhima,
                          paye_brackets=paye_brackets,
                          payroll_records=payroll_records,
                          current_month=current_month,
                          current_year=current_year,
                          department_labels=department_labels,
                          department_counts=department_counts,
                          branch_labels=branch_labels,
                          branch_counts=branch_counts,
                          recent_reports=recent_reports,
                          license_status=license_status,
                          license_expiry=license_expiry)


@app.route('/process_payroll', methods=['GET', 'POST'])
@login_required
def process_payroll():
    # Check for skip parameter in GET request
    if request.method == 'GET' and request.args.get('skip') != 'true':
        return redirect(url_for('payrun_verification'))
    
    # Get company and check license
    company = Company.query.first()
    if not company or not company.is_licensed:
        flash('Your license has expired. Please contact your administrator to renew the license.', 'danger')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        # Get form values with proper error handling
        try:
            year = int(request.form.get('year', 0))
            month = int(request.form.get('month', 0))
            
            if year == 0 or month == 0:
                flash('Please provide valid year and month values', 'danger')
                return redirect(url_for('process_payroll', skip='true'))
                
            # Check if payroll records already exist for this month/year
            existing_records = PayrollRecord.query.filter(
                extract('year', PayrollRecord.pay_date) == year,
                extract('month', PayrollRecord.pay_date) == month
            ).first()
            
            if existing_records:
                flash(f'Payroll records for {month}/{year} already exist!', 'warning')
                return redirect(url_for('payroll_records'))
            
            # Get selected employee IDs
            selected_employee_ids = request.form.getlist('selected_employees')
            
            if not selected_employee_ids:
                flash('No employees selected for payroll processing.', 'warning')
                return redirect(url_for('process_payroll', skip='true'))
            
            # Process payroll only for selected employees
            processed_count = 0
            pay_date = f"{year}-{month:02d}-01"
            
            for employee_id in selected_employee_ids:
                employee = Employee.query.get(employee_id)
                if employee and employee.is_active:
                    # Create new payroll record with employee's salary data
                    record = PayrollRecord(
                        employee=employee,
                        pay_date=pay_date,
                        basic_salary=employee.basic_salary or 0.0,
                        housing_allowance=employee.housing_allowance or 0.0,
                        lunch_allowance=employee.lunch_allowance or 0.0,
                        transport_allowance=employee.transport_allowance or 0.0,
                        overtime=0.0,  # Will be calculated later
                        gross_pay=0.0,  # Will be calculated later
                        paye=0.0,  # Will be calculated later
                        napsa=0.0,  # Will be calculated later
                        nhima=0.0,  # Will be calculated later
                        salary_advance=0.0,
                        rainbow_loan=0.0,
                        other_deduction=0.0,
                        other_deduction_reason=None,
                        total_deductions=0.0,  # Will be calculated later
                        net_pay=0.0  # Will be calculated later
                    )
                    
                    # Calculate initial values
                    record.gross_pay = (record.basic_salary + 
                                      record.housing_allowance + 
                                      record.lunch_allowance + 
                                      record.transport_allowance + 
                                      record.overtime)
                    
                    # Calculate deductions
                    record.paye = calculate_paye(record.gross_pay)
                    record.napsa = record.basic_salary * 0.05  # 5% of basic salary
                    record.nhima = record.basic_salary * 0.01  # 1% of basic salary
                    
                    # Calculate total deductions and net pay
                    record.total_deductions = (record.paye + 
                                             record.napsa + 
                                             record.nhima + 
                                             record.salary_advance + 
                                             record.rainbow_loan + 
                                             record.other_deduction)
                    
                    record.net_pay = record.gross_pay - record.total_deductions
                    
                    db.session.add(record)
                    processed_count += 1
            
            db.session.commit()
            flash(f'Payroll processed successfully for {processed_count} employees!', 'success')
            return redirect(url_for('payroll_records'))
        except ValueError:
            flash('Please provide valid year and month values', 'danger')
            return redirect(url_for('process_payroll', skip='true'))
    
    # For GET requests, show the payroll processing form
    employees = Employee.query.filter_by(is_active=True).all()
    
    # Group employees by department
    employees_by_dept = {}
    for employee in employees:
        dept = employee.department or 'Unassigned'
        if dept not in employees_by_dept:
            employees_by_dept[dept] = []
        employees_by_dept[dept].append(employee)
    
    # Get current date for default selections
    current_date = datetime.now()
    current_year = current_date.year
    current_month = current_date.month
    
    return render_template('process_payroll.html', 
                         employees=employees,
                         employees_by_dept=employees_by_dept,
                         current_year=current_year,
                         current_month=current_month)



@app.route('/payroll_records', methods=['GET', 'POST'])
@login_required
def payroll_records():
    # Get filter parameters
    year = request.args.get('year', str(datetime.now().year))
    month = request.args.get('month', '0')  # 0 means all months
    
    # Convert to integers
    try:
        year = int(year)
        month = int(month)
    except (ValueError, TypeError):
        year = datetime.now().year
        month = 0
    
    # Base query - join with Employee to get employee details
    query = PayrollRecord.query.join(Employee)
    
    # Apply filters - since pay_date is a string, we need to filter using string operations
    if year > 0:
        # Filter records by year - pay_date format is 'YYYY-MM-DD'
        query = query.filter(PayrollRecord.pay_date.startswith(f"{year}-"))
    
    if month > 0:
        # Filter records by month - pay_date format is 'YYYY-MM-DD'
        month_str = f"-{month:02d}-"  # Format with leading zero
        query = query.filter(PayrollRecord.pay_date.like(f"%{month_str}%"))
    
    # Get all records, ordered by date (newest first)
    records = query.order_by(PayrollRecord.pay_date.desc()).all()
    
    # Group records by month/year for display
    grouped_records = {}
    for record in records:
        date_parts = record.pay_date.split('-')
        if len(date_parts) >= 2:
            record_year = int(date_parts[0])
            record_month = int(date_parts[1])
            key = f"{record_year}-{record_month:02d}"
            if key not in grouped_records:
                grouped_records[key] = []
            grouped_records[key].append(record)
    
    # Get all available years for the dropdown
    # Since we can't use SQL functions reliably on string dates, we'll extract years from existing records
    available_years = set()
    all_records = PayrollRecord.query.all()
    for record in all_records:
        try:
            date_parts = record.pay_date.split('-')
            if len(date_parts) >= 1:
                year_val = int(date_parts[0])
                if year_val > 0:
                    available_years.add(year_val)
        except (ValueError, IndexError):
            continue
    
    # Convert to sorted list
    available_years = sorted(list(available_years), reverse=True)
    
    # If no valid years found or if available_years is empty, use default years
    if not available_years:
        available_years = [2020, 2021, 2022, 2023, 2024, 2025]
    
    # If the selected year is not in available_years, add it
    if year > 0 and year not in available_years:
        available_years.append(year)
        available_years.sort(reverse=True)
    
    # Get current year for the template
    current_year = datetime.now().year
    
    return render_template('payroll_records.html', 
                           records=records, 
                           grouped_records=grouped_records,
                           current_year=current_year,
                           selected_year=year,
                           selected_month=month,
                           available_years=available_years)
@app.route('/payrun_verification', methods=['GET', 'POST'])
@login_required
def payrun_verification():
    """Display payrun verification reports and allow generation of a new one."""
    if request.method == 'POST':
        # Generate the payrun verification report
        employees = get_active_employees_safely()
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        filename = f"payrun_verification_{timestamp}.pdf"
        reports_dir = os.path.join(app.static_folder, 'reports')
        os.makedirs(reports_dir, exist_ok=True)
        report_path = os.path.join(reports_dir, filename)
        relative_path = os.path.join('reports', filename)
        doc = SimpleDocTemplate(
            report_path,
            pagesize=(landscape(letter)[0] + 1.5*inch, landscape(letter)[1] + 0.5*inch),
            rightMargin=10,
            leftMargin=10,
            topMargin=20,
            bottomMargin=20
        )
        elements = []
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=12,
            spaceAfter=6,
            alignment=1
        )
        title = Paragraph(f"Payrun Verification Report - {datetime.now().strftime('%B %Y')}", title_style)
        elements.append(title)
        company_style = ParagraphStyle(
            'CompanyInfo',
            parent=styles['Normal'],
            fontSize=8,
            leading=10,
            alignment=1
        )
        company = Company.query.first()
        if company:
            elements.append(Paragraph(company.name, ParagraphStyle('CompanyName', parent=styles['Heading2'], alignment=1)))
            elements.append(Paragraph(company.address, company_style))
            elements.append(Paragraph(company.phone, company_style))
            elements.append(Paragraph(company.email, company_style))
            elements.append(Paragraph(f"Registration #: {company.registration_number}", company_style))
            elements.append(Paragraph(f"Tax #: {company.tax_number}", company_style))
            elements.append(Spacer(1, 6))
        data = [
            ['Code', 'Name', 'Position', 'Department', 'Branch', 'Basic Salary',
             'Housing', 'Lunch', 'Transport', 'Overtime', 'Gross Pay', 'PAYE', 'NAPSA', 'NHIMA', 'Net Pay']
        ]
        # Add employee data rows
        for employee in employees:
            # Handle branch name for both dictionary and ORM objects
            branch_name = ''
            if isinstance(employee, dict):
                # For dictionary objects from get_active_employees_safely()
                branch_id = employee.get('branch_id')
                if branch_id:
                    # Look up the branch name using the branch_id
                    branch = Branch.query.get(branch_id)
                    if branch:
                        branch_name = branch.name
                        
                # Access dictionary fields with get() method
                basic_salary = employee.get('basic_salary', 0) or 0
                housing = employee.get('housing_allowance', 0) or 0
                lunch = employee.get('lunch_allowance', 0) or 0
                transport = employee.get('transport_allowance', 0) or 0
                overtime = employee.get('overtime', 0) or 0
                gross_pay = basic_salary + housing + lunch + transport + overtime
                
                # Calculate tax values for dictionary objects
                # We need to create a temporary object with calculation methods
                class TempEmployee:
                    def __init__(self, basic_salary):
                        self.basic_salary = basic_salary
                    
                    def calculate_paye(self):
                        return calculate_paye(self.basic_salary)
                    
                    def calculate_napsa(self):
                        napsa_ceiling = 1221.80  # Maximum NAPSA contribution
                        contribution = self.basic_salary * 0.05
                        return min(contribution, napsa_ceiling)
                    
                    def calculate_nhima(self):
                        return self.basic_salary * 0.01
                
                temp_emp = TempEmployee(basic_salary)
                paye = temp_emp.calculate_paye()
                napsa = temp_emp.calculate_napsa()
                nhima = temp_emp.calculate_nhima()
                total_deductions = paye + napsa + nhima
                net_pay = gross_pay - total_deductions
            else:
                # For ORM objects - use attribute access
                branch_name = employee.branch.name if hasattr(employee, 'branch') and employee.branch else ''
                basic_salary = employee.basic_salary or 0
                housing = employee.housing_allowance or 0
                lunch = employee.lunch_allowance or 0
                transport = employee.transport_allowance or 0
                overtime = employee.overtime or 0
                gross_pay = basic_salary + housing + lunch + transport + overtime
                paye = employee.calculate_paye() if hasattr(employee, 'calculate_paye') else 0
                napsa = employee.calculate_napsa() if hasattr(employee, 'calculate_napsa') else 0
                nhima = employee.calculate_nhima() if hasattr(employee, 'calculate_nhima') else 0
                total_deductions = employee.calculate_total_deductions() if hasattr(employee, 'calculate_total_deductions') else 0
                net_pay = gross_pay - total_deductions
            # Build row data with proper handling for both dict and ORM objects
            if isinstance(employee, dict):
                # For dictionary objects
                row = [
                    employee.get('employee_code', ''),
                    employee.get('name', ''),
                    employee.get('position', ''),
                    employee.get('department', ''),
                    branch_name,
                    f"K {basic_salary:,.2f}",
                    f"K {housing:,.2f}",
                    f"K {lunch:,.2f}",
                    f"K {transport:,.2f}",
                    f"K {overtime:,.2f}",
                    f"K {gross_pay:,.2f}",
                    f"K {paye:,.2f}",
                    f"K {napsa:,.2f}",
                    f"K {nhima:,.2f}",
                    f"K {net_pay:,.2f}"
                ]
            else:
                # For ORM objects
                row = [
                    employee.employee_code,
                    employee.name,
                    employee.position,
                    employee.department,
                    branch_name,
                    f"K {basic_salary:,.2f}",
                    f"K {housing:,.2f}",
                    f"K {lunch:,.2f}",
                    f"K {transport:,.2f}",
                    f"K {overtime:,.2f}",
                    f"K {gross_pay:,.2f}",
                    f"K {paye:,.2f}",
                    f"K {napsa:,.2f}",
                    f"K {nhima:,.2f}",
                    f"K {net_pay:,.2f}"
                ]
            data.append(row)
        # Import required modules for table creation
        from reportlab.platypus import Table, TableStyle
        from reportlab.lib import colors
        # Define column widths (adjust as needed for your data)
        col_widths = [35, 70, 50, 50, 60, 48, 48, 40, 48, 48, 50, 40, 40, 40, 55]
        # Calculate totals for each numeric column with proper handling for both dict and ORM objects
        total_basic = 0
        total_housing = 0
        total_lunch = 0
        total_transport = 0
        total_overtime = 0
        total_gross = 0
        total_paye = 0
        total_napsa = 0
        total_nhima = 0
        total_deductions = 0
        
        # Calculate totals with proper handling for both dictionary and ORM objects
        for employee in employees:
            if isinstance(employee, dict):
                # For dictionary objects
                basic = employee.get('basic_salary', 0) or 0
                housing = employee.get('housing_allowance', 0) or 0
                lunch = employee.get('lunch_allowance', 0) or 0
                transport = employee.get('transport_allowance', 0) or 0
                overtime = employee.get('overtime', 0) or 0
                
                # Create a temporary employee object for tax calculations
                class TempEmployee:
                    def __init__(self, basic_salary):
                        self.basic_salary = basic_salary
                    
                    def calculate_paye(self):
                        return calculate_paye(self.basic_salary)
                    
                    def calculate_napsa(self):
                        napsa_ceiling = 1221.80  # Maximum NAPSA contribution
                        contribution = self.basic_salary * 0.05
                        return min(contribution, napsa_ceiling)
                    
                    def calculate_nhima(self):
                        return self.basic_salary * 0.01
                
                temp_emp = TempEmployee(basic)
                paye = temp_emp.calculate_paye()
                napsa = temp_emp.calculate_napsa()
                nhima = temp_emp.calculate_nhima()
                deductions = paye + napsa + nhima
            else:
                # For ORM objects
                basic = employee.basic_salary or 0
                housing = employee.housing_allowance or 0
                lunch = employee.lunch_allowance or 0
                transport = employee.transport_allowance or 0
                overtime = employee.overtime or 0
                paye = employee.calculate_paye() if hasattr(employee, 'calculate_paye') else 0
                napsa = employee.calculate_napsa() if hasattr(employee, 'calculate_napsa') else 0
                nhima = employee.calculate_nhima() if hasattr(employee, 'calculate_nhima') else 0
                deductions = employee.calculate_total_deductions() if hasattr(employee, 'calculate_total_deductions') else (paye + napsa + nhima)
            
            # Add to totals
            gross = basic + housing + lunch + transport + overtime
            
            total_basic += basic
            total_housing += housing
            total_lunch += lunch
            total_transport += transport
            total_overtime += overtime
            total_gross += gross
            total_paye += paye
            total_napsa += napsa
            total_nhima += nhima
            total_deductions += deductions
        
        total_net = total_gross - total_deductions
        # Append the totals row
        totals_row = [
            "TOTALS", "", "", "", "",  # Code, Name, Position, Department, Branch
            f"K {total_basic:,.2f}",
            f"K {total_housing:,.2f}",
            f"K {total_lunch:,.2f}",
            f"K {total_transport:,.2f}",
            f"K {total_overtime:,.2f}",
            f"K {total_gross:,.2f}",
            f"K {total_paye:,.2f}",
            f"K {total_napsa:,.2f}",
            f"K {total_nhima:,.2f}",
            f"K {total_net:,.2f}"
        ]
        data.append(totals_row)
        # Update the table with the new data
        table = Table(data, repeatRows=1, colWidths=col_widths)
        table_style = TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 6),
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 1),
            ('LEFTPADDING', (0, 0), (-1, -1), 1),
            ('RIGHTPADDING', (0, 0), (-1, -1), 1),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ])
        totals_row_index = len(data) - 1
        table_style.add('BACKGROUND', (0, totals_row_index), (-1, totals_row_index), colors.lightgrey)
        table.setStyle(table_style)
        elements.append(table)
        # Add signature spaces for verification
        elements.append(Spacer(1, 20))
        
        # Add centered signature lines
        styles.add(ParagraphStyle(name='SignatureStyle', alignment=1))  # 1 = center alignment
        elements.append(Paragraph("Prepared By: ______________________________", styles['SignatureStyle']))
        elements.append(Spacer(1, 20))
        elements.append(Paragraph("Approved By: ______________________________", styles['SignatureStyle']))
        
        # Add page numbers
        from reportlab.pdfgen import canvas
        def add_page_number(canvas, doc):
            page_num_text = f"Page {canvas.getPageNumber()}"
            canvas.setFont('Helvetica', 8)
            canvas.drawRightString(800, 10, page_num_text)
        doc.build(elements, onFirstPage=add_page_number, onLaterPages=add_page_number)
        # Build the PDF
        print(f"[DEBUG] Number of active employees: {len(employees)}")
        print(f"[DEBUG] Number of rows in data table (including header): {len(data)}")
        # Save report to database
        report_id = f"payrun_verification_{timestamp}"
        report = Report(
            id=report_id,
            name=f"Payrun Verification Report - {datetime.now().strftime('%B %Y')}",
            type='verification',
            format='pdf',
            generated_at=datetime.now(),
            year=datetime.now().year,
            month=datetime.now().month,
            file_path=relative_path
        )
        db.session.add(report)
        db.session.commit()
        flash('Payrun verification report generated successfully!', 'success')
        return redirect(url_for('view_report_html', id=report_id))
    reports = Report.query.filter_by(type='verification').order_by(Report.generated_at.desc()).all()
    return render_template('payrun_verification.html', reports=reports)

@app.route('/debug/active_employees')
@login_required
def debug_active_employees():
    employees = Employee.query.all()
    output = [
        f"ID: {e.id}, Name: {e.name}, is_active: {e.is_active}" for e in employees
    ]
    return '<br>'.join(output) or 'No employees found.'

if __name__ == '__main__':
    with app.app_context():
        # Check for restart flag
        restart_flag_file = os.path.join(app.root_path, 'restart.flag')
        if os.path.exists(restart_flag_file):
            try:
                # Remove the flag file
                os.remove(restart_flag_file)
                print("Restart flag detected and removed. Reinitializing database connection.")
                
                # Force reconnection to the database
                db.session.remove()
                db.engine.dispose()
                
                # Wait a moment to ensure file operations are complete
                import time
                time.sleep(1)
                
                # Reconnect to the database with a fresh engine
                from sqlalchemy import create_engine
                from sqlalchemy.orm import scoped_session, sessionmaker
                
                db_uri = f"sqlite:///{os.path.join(app.root_path, 'instance', 'payroll.db')}"
                new_engine = create_engine(db_uri)
                db.engine = new_engine
                db.session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=new_engine))
                
                # Ensure all models are properly initialized
                db.Model.metadata.clear()
                db.create_all()
                
                print("Database connection reinitialized successfully.")
            except Exception as e:
                print(f"Error during database reinitialization: {str(e)}")
        
        # Create tables if they don't exist (don't drop existing tables)
        db.create_all()
        
        # Create default admin user if it doesn't exist
        admin = User.query.filter_by(username='admin').first()
        if not admin:
            admin = User(username='admin', password=generate_password_hash('admin123'), is_admin=True, created_at=datetime.utcnow())
            db.session.add(admin)
            db.session.commit()
            
        # Create default tax settings if they don't exist
        tax_settings = TaxSettings.query.first()
        if not tax_settings:
            tax_settings = TaxSettings(
                bracket1=0,    # 0% up to K5,100
                bracket2=20,   # 20% K5,100.01 - K7,100
                bracket3=30,   # 30% K7,100.01 - K9,200
                bracket4=37    # 37% above K9,200
            )
            db.session.add(tax_settings)
            db.session.commit()
            
    app.run()



@app.route('/email_payslip/<int:record_id>', methods=['POST'])
@login_required
def email_payslip(record_id):
    record = PayrollRecord.query.get_or_404(record_id)
    
    # Check if employee has an email address
    if not record.employee.email:
        flash('Employee does not have an email address.', 'error')
        return redirect(url_for('payroll_records'))
    
    try:
        # Generate the PDF payslip
        pdf_content = generate_payslip_pdf(record)
        
        # Send email with PDF attachment
        send_email(
            to=record.employee.email,
            subject=f"Payslip for {record.pay_date}",
            body=f"Dear {record.employee.name},\n\nPlease find attached your payslip for {record.pay_date}.\n\nRegards,\nHR Department",
            attachment=pdf_content,
            filename=f"payslip_{record.employee.employee_code}_{record.pay_date}.pdf"
        )
        
        flash(f'Payslip sent to {record.employee.email} successfully!', 'success')
    except Exception as e:
        flash(f'Error sending payslip: {str(e)}', 'error')
    
    return redirect(url_for('payroll_records'))

# Helper function to generate payslip PDF
def generate_payslip_pdf(record):
    company = Company.query.first()
    
    # Create a BytesIO buffer for the PDF
    buffer = BytesIO()
    
    # Create the PDF document with better styling
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Center', alignment=1))
    styles.add(ParagraphStyle(name='Right', alignment=2))
    styles.add(ParagraphStyle(name='PayslipTitle', fontName='Helvetica-Bold', fontSize=16, alignment=1, spaceAfter=12))
    styles.add(ParagraphStyle(name='SubHeading', fontName='Helvetica-Bold', fontSize=10))
    
    # Create a table for the header with company info on left and payslip info on right
    header_data = []
    
    # Left column - Company info
    company_info = []
    if company and company.logo:
        logo_path = os.path.join(app.static_folder, company.logo)
        if os.path.exists(logo_path):
            img = Image(logo_path)
            img.drawHeight = 60
            img.drawWidth = 120
            company_info.append(img)
    
    if company:
        company_info.append(Paragraph(f"<b>{company.name}</b>", styles['Heading3']))
        company_info.append(Paragraph(company.address, styles['Normal']))
        company_info.append(Paragraph(f"Phone: {company.phone}", styles['Normal']))
        company_info.append(Paragraph(f"Email: {company.email}", styles['Normal']))
        company_info.append(Paragraph(f"Registration #: {company.registration_number}", styles['Normal']))
        company_info.append(Paragraph(f"Tax #: {company.tax_number}", styles['Normal']))
    
    # Right column - Payslip info with improved styling
    payslip_info = []
    payslip_info.append(Paragraph("<font color='blue'><b>PAYSLIP</b></font>", styles['PayslipTitle']))
    
    # Format the pay date properly
    pay_month = ""
    try:
        if isinstance(record.pay_date, str):
            # Try to parse the date string
            pay_date = datetime.strptime(record.pay_date, '%Y-%m-%d')
            pay_month = pay_date.strftime('%B %Y')
        else:
            pay_month = record.pay_date.strftime('%B %Y')
    except:
        pay_month = record.pay_date[0:7]
    
    payslip_info.append(Paragraph(f"For the month of <b>{pay_month}</b>", styles['Normal']))
    payslip_info.append(Paragraph(f"Pay Date: <b>{record.pay_date}</b>", styles['Normal']))
    
    # Add company and payslip info to the header table
    header_data.append([company_info, payslip_info])
    
    # Create the header table with better spacing
    header_table = Table(header_data, colWidths=[doc.width/2.0]*2)
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 20))
    
    # Employee Information in a styled box with improved colors
    elements.append(Paragraph("<b>Employee Details</b>", styles['Heading4']))
    employee_data = [
        [Paragraph(f"<b>Name:</b> {record.employee.name}", styles['Normal']), 
         Paragraph(f"<b>Employee Code:</b> {record.employee.employee_code}", styles['Normal'])],
        [Paragraph(f"<b>Position:</b> {record.employee.position}", styles['Normal']), 
         Paragraph(f"<b>Email:</b> {record.employee.email}", styles['Normal'])],
        [Paragraph(f"<b>Department:</b> {record.employee.department}", styles['Normal']), 
         Paragraph(f"<b>Branch:</b> {record.employee.branch.name if record.employee.branch else ''}", styles['Normal'])]
    ]
    # Match the employee table width to the combined table width that will be used later
    employee_table = Table(employee_data, colWidths=[doc.width/2.0]*2)
    employee_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(employee_table)
    elements.append(Spacer(1, 20))
    
    # Create tables for earnings and deductions side by side with modern colors
    earnings_data = [
        [Paragraph("<b>Earnings</b>", styles['SubHeading']), Paragraph("<b>Amount</b>", styles['Right'])],
        ['Basic Salary', Paragraph(f"K {record.basic_salary:,.2f}", styles['Right'])],
        ['Housing Allowance', Paragraph(f"K {record.housing_allowance:,.2f}", styles['Right'])],
        ['Lunch Allowance', Paragraph(f"K {record.lunch_allowance:,.2f}", styles['Right'])],
        ['Transport Allowance', Paragraph(f"K {record.transport_allowance:,.2f}", styles['Right'])],
        ['Overtime', Paragraph(f"K {record.overtime:,.2f}", styles['Right'])],
        [Paragraph("<b>Gross Pay:</b>", styles['SubHeading']), Paragraph(f"<b>K {record.gross_pay:,.2f}</b>", styles['Right'])]
    ]
    
    deductions_data = [
        [Paragraph("<b>Deductions</b>", styles['SubHeading']), Paragraph("<b>Amount</b>", styles['Right'])],
        ['PAYE', Paragraph(f"K {record.paye:,.2f}", styles['Right'])],
        ['NAPSA', Paragraph(f"K {record.napsa:,.2f}", styles['Right'])],
        ['NHIMA', Paragraph(f"K {record.nhima:,.2f}", styles['Right'])],
        ['Salary Advance', Paragraph(f"K {record.salary_advance:,.2f}", styles['Right'])],
        ['Rainbow Loan', Paragraph(f"K {record.rainbow_loan:,.2f}", styles['Right'])],
        ['Other Deduction', Paragraph(f"K {record.other_deduction:,.2f}", styles['Right'])],
        [Paragraph("<b>Total Deductions:</b>", styles['SubHeading']), Paragraph(f"<b>K {record.total_deductions:,.2f}</b>", styles['Right'])]
    ]
    
    # Table styles with modern colors
    earnings_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.green),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, -1), (-1, -1), colors.lightgreen),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 8),
    ])
    
    deductions_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.red),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, -1), (-1, -1), colors.pink),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 8),
    ])
    
    # Create the tables with widths that align with the employee table
    # Use 45% of doc width for content and 5% for spacing on each table
    earnings_table = Table(earnings_data, colWidths=[(doc.width*0.45)*0.7, (doc.width*0.45)*0.3])
    earnings_table.setStyle(earnings_style)
    
    deductions_table = Table(deductions_data, colWidths=[(doc.width*0.45)*0.7, (doc.width*0.45)*0.3])
    deductions_table.setStyle(deductions_style)
    
    # Create a table to hold both earnings and deductions side by side with spacing between
    combined_data = [[earnings_table, deductions_table]]
    combined_table = Table(combined_data, colWidths=[doc.width*0.5, doc.width*0.5])
    combined_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (0, 0), 0),  # No padding on left side of earnings table
        ('RIGHTPADDING', (0, 0), (0, 0), doc.width*0.05),  # 5% spacing between tables
        ('LEFTPADDING', (1, 0), (1, 0), doc.width*0.05),  # 5% spacing between tables
        ('RIGHTPADDING', (1, 0), (1, 0), 0),  # No padding on right side of deductions table
    ]))
    elements.append(combined_table)
    
    elements.append(Spacer(1, 25))
    
    # Net Pay in a highlighted box with improved styling
    net_pay_data = [[Paragraph("<font color='white'><b>Net Pay</b></font>", styles['Heading3']), 
                    Paragraph(f"<font color='white'><b>K {record.net_pay:,.2f}</b></font>", styles['Heading2'])]]
    net_pay_table = Table(net_pay_data, colWidths=[doc.width/2.0, doc.width/2.0])
    net_pay_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.blue),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (0, 0), 14),
        ('FONTSIZE', (1, 0), (1, 0), 18),
        ('PADDING', (0, 0), (-1, -1), 15),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    elements.append(net_pay_table)
    
    # Add footer with date and signature
    elements.append(Spacer(1, 30))
    footer_text = f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M')} | This is a computer-generated document and requires no signature."
    elements.append(Paragraph(footer_text, styles['Normal']))
    
    # Build PDF
    doc.build(elements)
    
    # Get the PDF content
    pdf_content = buffer.getvalue()
    buffer.close()
    
    return pdf_content

# Helper function to send email with attachment
def send_email(to, subject, body, attachment=None, filename=None):
    # Get company settings
    company = Company.query.first()
    if not company or not company.email_server or not company.email_port or not company.email_username or not company.email_password:
        raise ValueError("Email settings not configured. Please update company settings.")
    
    # Create message
    msg = MIMEMultipart()
    msg['From'] = company.email_username
    msg['To'] = to
    msg['Subject'] = subject
    
    # Attach body
    msg.attach(MIMEText(body, 'plain'))
    
    # Attach PDF if provided
    if attachment and filename:
        attachment_part = MIMEBase('application', 'octet-stream')
        attachment_part.set_payload(attachment)
        encoders.encode_base64(attachment_part)
        attachment_part.add_header('Content-Disposition', f'attachment; filename={filename}')
        msg.attach(attachment_part)
    
    # Send email
    try:
        server = smtplib.SMTP(company.email_server, company.email_port)
        server.starttls()
        server.login(company.email_username, company.email_password)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        raise Exception(f"Failed to send email: {str(e)}")

