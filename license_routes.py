from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from models import db, License, Company
import secrets
import string
import hashlib

license_bp = Blueprint('license', __name__)

def generate_license_key():
    """
    Generate a secure annual license key with the following format:
    XXXX-XXXX-XXXX-XXXX-YYYY
    Where XXXX are random alphanumeric characters and YYYY is the year
    """
    # Generate 4 groups of 4 random alphanumeric characters
    chars = string.ascii_uppercase + string.digits
    groups = [''.join(secrets.choice(chars) for _ in range(4)) for _ in range(4)]
    
    # Add the current year
    current_year = datetime.now().year
    year_str = str(current_year)
    
    # Combine all parts with hyphens
    license_key = '-'.join(groups + [year_str])
    
    return license_key

@license_bp.route('/licenses')
@login_required
def licenses():
    if not current_user.is_admin:
        flash('You do not have permission to access this page.', 'danger')
        return redirect(url_for('dashboard'))
    
    licenses = License.query.all()
    return render_template('licenses.html', licenses=licenses)

@license_bp.route('/add_license', methods=['GET', 'POST'])
@login_required
def add_license():
    if not current_user.is_admin:
        flash('You do not have permission to access this page.', 'danger')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        license_key = request.form.get('license_key')
        end_date = datetime.strptime(request.form.get('end_date'), '%Y-%m-%d')
        
        # Get the first company (assuming single company setup)
        company = Company.query.first()
        
        if not company:
            flash('No company found. Please set up company details first.', 'danger')
            return redirect(url_for('company_settings'))
        
        new_license = License(
            company_id=company.id,
            license_key=license_key,
            end_date=end_date
        )
        
        try:
            db.session.add(new_license)
            db.session.commit()
            flash('License added successfully!', 'success')
            return redirect(url_for('license.licenses'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding license: {str(e)}', 'danger')
    
    # Generate a new license key for the form
    generated_key = generate_license_key()
    return render_template('add_license.html', generated_key=generated_key)

@license_bp.route('/deactivate_license/<int:license_id>', methods=['POST'])
@login_required
def deactivate_license(license_id):
    if not current_user.is_admin:
        flash('You do not have permission to perform this action.', 'danger')
        return redirect(url_for('dashboard'))
    
    license = License.query.get_or_404(license_id)
    license.is_active = False
    
    try:
        db.session.commit()
        flash('License deactivated successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deactivating license: {str(e)}', 'danger')
    
    return redirect(url_for('license.licenses'))

@license_bp.route('/activate_license/<int:license_id>', methods=['POST'])
@login_required
def activate_license(license_id):
    if not current_user.is_admin:
        flash('You do not have permission to perform this action.', 'danger')
        return redirect(url_for('dashboard'))
    
    license = License.query.get_or_404(license_id)
    
    # Deactivate all other licenses for this company
    License.query.filter_by(company_id=license.company_id).update({'is_active': False})
    
    # Activate the selected license
    license.is_active = True
    
    try:
        db.session.commit()
        flash('License activated successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error activating license: {str(e)}', 'danger')
    
    return redirect(url_for('license.licenses'))
