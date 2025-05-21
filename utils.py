from models import TaxSettings

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