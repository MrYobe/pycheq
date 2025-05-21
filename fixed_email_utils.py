import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

def send_smtp_email(smtp_server, smtp_port, use_tls, username, password, sender, recipients, subject, html_body, attachments=None):
    """
    Send an email using SMTP with proper SSL/TLS handling
    
    Parameters:
    - smtp_server: SMTP server address (e.g., smtp.gmail.com)
    - smtp_port: SMTP server port (e.g., 587 for TLS, 465 for SSL)
    - use_tls: Whether to use TLS (ignored for port 465 which uses SSL)
    - username: SMTP username for authentication
    - password: SMTP password for authentication
    - sender: Email sender address
    - recipients: List of recipient email addresses or a single email address
    - subject: Email subject
    - html_body: HTML content of the email
    - attachments: Optional list of (filename, content) tuples
    
    Returns:
    - (success, message) tuple where success is a boolean and message is a string
    """
    try:
        # Ensure recipients is a list
        if isinstance(recipients, str):
            recipients = [recipients]
        
        # Create message
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = ', '.join(recipients)
        
        # Attach HTML body
        msg.attach(MIMEText(html_body, 'html'))
        
        # Attach files if provided
        if attachments:
            for filename, content in attachments:
                attachment = MIMEApplication(content)
                attachment['Content-Disposition'] = f'attachment; filename="{filename}"'
                msg.attach(attachment)
        
        # Connect to SMTP server
        print(f"Connecting to SMTP server: {smtp_server}:{smtp_port}")
        
        # Use SSL for port 465, otherwise use standard SMTP with optional TLS
        if smtp_port == 465:
            print("Using SSL connection")
            smtp = smtplib.SMTP_SSL(smtp_server, smtp_port)
        else:
            smtp = smtplib.SMTP(smtp_server, smtp_port)
            smtp.ehlo()
            
            # Use TLS if enabled
            if use_tls:
                print("Starting TLS")
                smtp.starttls()
                smtp.ehlo()
        
        # Login if credentials provided
        if username and password:
            print(f"Logging in as {username}")
            smtp.login(username, password)
        
        # Send email
        print(f"Sending email to {recipients}")
        smtp.send_message(msg)
        
        # Close connection
        smtp.quit()
        print("Email sent successfully!")
        return True, "Email sent successfully"
    
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        error_message = f"Error sending email: {str(e)}\n{error_trace}"
        print(error_message)
        return False, str(e)
