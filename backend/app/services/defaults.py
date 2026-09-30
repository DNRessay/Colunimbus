# Default categories seeded for every new practice. Keywords are matched as substrings of the
# (lowercased) bank description; the practice edits them from the Categories page.
DEFAULT_CATEGORIES = [
    ('Groceries', 'debit', 4, 'supermarket,checkers,woolworths,pick n pay,spar,shoprite,food lover,clicks food,spaza,tucksho,mart,makro,game store,usave,s2s,ccn,tuck sho,tuck shop,s2s*,ccn*,alcohol', 'checkers,woolworths,spar,shoprite,pnp,foodlovers'),
    ('Fuel', 'debit', 1, 'caltex,shell,sasol,engen,total,bp,astron,fuel,petrol', 'caltex,shell,sasol,engen,total,bp'),
    ('Transport', 'debit', 2, 'uber,bolt,taxi,gautrain,metrobus,intercape,greyhound,ride,trip', 'uber,bolt'),
    ('Food & Dining', 'debit', 3, "nando,kfc,mcdonalds,mcdonald,steers,wimpy,debonairs,fishaways,chicken licken,burger king,roman's,pizza,restaurant,cafe,bakery,coffee,mugg,bean,ocean basket", 'nandos,kfc,mcdonalds,steers,wimpy,debonairs'),
    ('Entertainment', 'debit', 5, 'netflix,showmax,dstv,spotify,apple music,youtube,amazon prime,hulu,disney,cinema,ster-kinekor,nu metro,gaming,playstation,xbox', 'netflix,showmax,dstv,spotify'),
    ('Healthcare', 'debit', 6, 'dischem,pharmacy,clicks,clinic,hospital,doctor,dentist,optometrist,medirite,medihelp,discovery health,bonitas,momentum health', 'dischem,clicks'),
    ('Telecommunications', 'debit', 7, 'vodacom,mtn,telkom,cell c,airtime,data,prepaid,recharge,rain,afrihost,webafrica', 'vodacom,mtn,telkom,cellc'),
    ('Banking & Finance', 'debit', 8, 'fnb,absa,nedbank,standard bank,capitec,investec,african bank,service fee,bank charge,atm fee,monthly fee,admin fee,interest charged,insurance premium,monthly account admin,branch card replacement,print statement,print statement fee,external payment,banking app', 'fnb,absa,nedbank,capitec'),
    ('Bank Charges', 'debit', 13, 'set-off,setoff,sms payment notification,sms notification,stop payment,unpaid debit,dishonour,penalty fee,returned item,early settlement,account maintenance,debit order fee,sms fee,card fee,statement fee,dispute fee,debicheck insufficient funds,eft debit order insufficient funds,debicheck authentication,insufficient funds fee,debit order return,unpaid debit order,debicheck,eft debit order,debicheck insufficient,eft debit order insufficient,insufficient funds', 'setoff,sms,stop payment,debicheck,insufficient funds'),
    ('Utilities', 'debit', 9, 'eskom,city power,municipality,rates,water,electricity,prepaid electricity,sanitation,refuse,tshwane,joburg,ekurhuleni,cape town metro,water rates', 'eskom,tshwane,joburg'),
    ('Shopping', 'debit', 10, 'takealot,amazon,mr price,ackermans,pep,jet,woolworths clothing,h&m,zara,edgars,truworths,foschini,sportsmans,outdoor,builders,leroy merlin', 'takealot,mrprice,ackermans,pep'),
    ('Income', 'credit', 11, 'salary,payroll,wages,payment received,transfer received,received from,transfer in,deposit,commission,bonus,dividend,refund,payshap payment received,payshap received,payshap', 'salary,payroll,payshap'),
    ('Interest Income', 'credit', 15, 'earned interest,interest earned,interest credited,interest paid,savings interest,interest income,interest', 'interest,earned interest'),
    ('Savings & Transfers', 'credit', 16, 'transfer from current,transfer from savings,transfer from cheque,internal transfer,transfer between accounts,transfer from account', 'transfer from current,internal transfer'),
    ('Digital Payments', 'debit', 17, 'client care immediate payment,immediate payment,digital payment,online payment,internet payment,card purchase online,e-payment', 'immediate payment,digital payment'),
    ('Savings Round-up', 'debit', 14, 'live better,round-up,round up,live better round-up,live better transfer,savings round', 'livebetter,roundup'),
    ('Transfer Out', 'debit', 12, 'transfer to,send money,ewallet,capitec pay,fnb pay,snapscan,zapper,payfast,peach payments,immediate payment,payshap send', 'ewallet,snapscan'),
    ('Intercompany Transfer', 'any', 18, '', ''),
]
